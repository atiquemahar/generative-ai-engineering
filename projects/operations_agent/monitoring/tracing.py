# projects/operations_agent/monitoring/tracing.py
# Day 45 — Foundry Tracing + Observability
#
# GraphTracer: wraps node and LLM calls with OpenTelemetry spans.
#
# Behaviour matrix:
#   APPLICATIONINSIGHTS_CONNECTION_STRING set + azure-monitor-opentelemetry installed
#       → spans exported to Azure Application Insights (visible in Foundry dashboard)
#   APPLICATIONINSIGHTS_CONNECTION_STRING not set, opentelemetry installed
#       → spans exported to console (local dev / CI)
#   opentelemetry not installed at all
#       → null OTEL, but timing is still recorded in-process via SpanData
#
# Design:
#   GraphTracer is instantiated once per process (lazy singleton via get_tracer()).
#   Each node or LLM call opens a span with the `span()` context manager.
#   SpanData is always populated (latency_ms, attributes) regardless of OTEL.
#   traced_llm_call() wraps a LangChain LLM .invoke() and extracts token counts
#   from response.response_metadata automatically.
#
# Usage — in a graph node:
#
#     with get_tracer().span("propose_action", user_role=user_role) as s:
#         response = llm.invoke(messages)
#         s.set_token_counts(response.response_metadata or {})
#
#     tracing_data = state.get("tracing_data") or {}
#     tracing_data["propose_action_latency_ms"] = s.latency_ms
#     tracing_data["propose_input_tokens"]  = s.attributes.get("input_tokens", 0)
#     tracing_data["propose_output_tokens"] = s.attributes.get("output_tokens", 0)
#
# OR the higher-level helper:
#
#     result = get_tracer().traced_llm_call("propose_action", llm, messages)
#     # result: {response, latency_ms, input_tokens, output_tokens}

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator

logger = logging.getLogger(__name__)

# ── Optional import: opentelemetry ───────────────────────────────────────────
# The SDK is NOT in requirements.txt — we degrade gracefully to timing-only mode.
try:
    from opentelemetry import trace as otel_trace       #type ignore
    from opentelemetry.sdk.resources import Resource    #type ignore
    from opentelemetry.sdk.trace import TracerProvider  #type ignore
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    logger.debug("opentelemetry-sdk not installed — GraphTracer will record timing in-process only")

# ── Optional import: azure-monitor-opentelemetry ─────────────────────────────
try:
    from azure.monitor.opentelemetry import configure_azure_monitor #type ignore

    _AZURE_MONITOR_AVAILABLE = True
except ImportError:
    _AZURE_MONITOR_AVAILABLE = False
    logger.debug("azure-monitor-opentelemetry not installed — Azure Monitor export disabled")

# ── SpanData ─────────────────────────────────────────────────────────────────

@dataclass
class SpanData:
    """
    In-process record of a single traced operation.
 
    Always populated — whether or not OTEL is installed.
    Writen by the `span()` context manager; consumed by the caller
    to update state["tracing_data"] and eventually the audit_logs columns.
    """
    name: str
    latency_ms: float = 0.0
    attributes: dict = field(default_factory=dict)
    _start: float = field(default_factory=time.perf_counter)

    def set_token_counts(self, response_metadata: dict, usage_metadata: dict = None) -> None:
        # Prefer usage_metadata (direct on response) — cleaner path for LangChain 0.3+
        if usage_metadata:
            self.attributes["input_tokens"]  = usage_metadata.get("input_tokens", 0)
            self.attributes["output_tokens"] = usage_metadata.get("output_tokens", 0)
            self.attributes["total_tokens"]  = usage_metadata.get("total_tokens", 0)
            return
        # Fallback: token_usage inside response_metadata
        usage = response_metadata.get("token_usage") or response_metadata.get("usage") or {}
        self.attributes["input_tokens"]  = usage.get("prompt_tokens", 0)
        self.attributes["output_tokens"] = usage.get("completion_tokens", 0)
        self.attributes["total_tokens"]  = usage.get("total_tokens", 0)

    def finish(self) -> None:
        """Record elapsed time.  Called by the context manager's finally block."""
        self.latency_ms = (time.perf_counter() - self._start) * 1000

# ── GraphTracer ───────────────────────────────────────────────────────────────
class GraphTracer:
    """
    OpenTelemetry-compatible tracer for the operations agent graph.
 
    One instance per process via `get_tracer()`.
    All public methods are exception-safe — a tracing failure never
    propagates to the caller.
    """
    SERVICE_NAME = "operations-agent"

    def __init__(self) -> None:
        self._otel_tracer: Any | None = None
        self._recorded_spans: list[SpanData] = []
        self._setup_provider()

    # ── OTEL provider setup ───────────────────────────────────────────────────

    def _setup_provider(self) -> None:
        """
        Configure the OTEL TracerProvider once.
        Never raises — failures are logged at WARNING level.
        """ 
        if not _OTEL_AVAILABLE:
            return

        conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()

        try:
            if conn_str and _AZURE_MONITOR_AVAILABLE:
                # Azure Monitor export — traces appear in the Foundry dashboard.
                # configure_azure_monitor sets the global TracerProvider.
                configure_azure_monitor(connection_string=conn_str) # type: ignore[name-defined]
                self._otel_tracer = otel_trace.get_tracer(self.SERVICE_NAME)
                logger.info(
                    "GraphTracer: Azure Monitor export configured — "
                    "traces will appear in Foundry dashboard"
                )
            else:
                # Console exporter — useful for local dev and CI.
                resource = Resource.create({"service.name": self.SERVICE_NAME})
                provider = TracerProvider(resource=resource) 
                exporter = ConsoleSpanExporter()
                processor = BatchSpanProcessor(exporter)  
                provider.add_span_processor(processor)
                otel_trace.set_tracer_provider(provider) 
                self._otel_tracer = otel_trace.get_tracer(self.SERVICE_NAME)
                logger.debug(
                    "GraphTracer: console exporter configured — "
                    "set APPLICATIONINSIGHTS_CONNECTION_STRING for Azure Monitor"
                )
        except Exception as exc:
            logger.warning(
                "GraphTracer: OTEL provider setup failed (%s) — "
                "timing-only mode active",
                exc,
            ) 
            self._otel_tracer = None

    # ── Public context manager ────────────────────────────────────────────────

    @contextmanager
    def span(self, name: str, **extra_attrs: Any) -> Generator[SpanData, None, None]:
        """
        Context manager that records latency and optional attributes.
 
        With OTEL: opens a real span, exports to Azure Monitor or console.
        Without OTEL: records timing in-process via SpanData only.
 
        Always:
          - SpanData.latency_ms is set on exit
          - SpanData is appended to self._recorded_spans
          - Exceptions raised inside the block propagate normally
 
        Example:
            with tracer.span("retrieve_policy_evidence", intent=intent) as s:
                policy_evidence = knowledge_agent.ask(policy_question)
            # s.latency_ms is now set; use it to update tracing_data
        """
        span_data = SpanData(name=name, attributes=dict(extra_attrs))

        if self._otel_tracer is not None:
            with self._otel_tracer.start_as_current_span(name) as otel_span:
                for k, v in extra_attrs.items():
                    otel_span.set_attribute(k, str(v))
                try:
                    yield span_data
                finally:
                    span_data.finish()
                    # Push final attributes to the OTEL span before it closes
                    otel_span.set_attribute("latency_ms", round(span_data.latency_ms, 2))
                    for k, v in span_data.attributes.items():
                        otel_span.set_attribute(str(k), str(v)) 
        else:
            # Null OTEL path — timing only
            try:
                yield span_data
            finally:
                span_data.finish()
        self._recorded_spans.append(span_data)

    # ── Convenience wrapper for LLM calls ────────────────────────────────────
    def traced_llm_call(self, span_name: str, llm: Any, messages: list, **extra_attrs: Any,) -> dict:
        """
        Wrap a LangChain LLM `.invoke(messages)` with a span.
 
        Extracts token counts from response.response_metadata automatically.
 
        Returns:
            {
                "response":      LangChain AIMessage,
                "latency_ms":    float,
                "input_tokens":  int,
                "output_tokens": int,
            }
 
        The response is returned so the caller can continue processing it
        (parse JSON, extract content, etc.) exactly as before.
        """
        span_data_ref: SpanData | None = None

        with self.span(span_name, **extra_attrs) as s:
            span_data_ref = s
            response = llm.invoke(messages)
            s.set_token_counts(
                response.response_metadata or {},
                usage_metadata=getattr(response, "usage_metadata", None),
            )

        assert span_data_ref is not None    # always set

        return {                                      # ← add this return
            "response":      response,
            "latency_ms":    span_data_ref.latency_ms,
            "input_tokens":  span_data_ref.attributes.get("input_tokens", 0),
            "output_tokens": span_data_ref.attributes.get("output_tokens", 0),
        }


    # ── Introspection (useful for tests and local dashboard) ─────────────────

    def get_recorded_spans(self) -> list[SpanData]:
        """
        Return a snapshot of all spans recorded in this process since startup
        (or since the last `clear_spans()` call).
 
        Useful for tests and for local metric dashboards that don't have
        Azure Monitor.
        """
        return list(self._recorded_spans) 

    def clear_spans(self) -> None:
        """Clear the in-process span list.  Useful between test cases."""
        self._recorded_spans.clear()

    @property
    def backend(self) -> str:
        """Human-readable description of the active export backend."""
        if self._otel_tracer is None:
            return "null (timing-only)"
        conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "") 
        if conn and _AZURE_MONITOR_AVAILABLE:
            return "azure-monitor"
        return "console" 

# ── Module-level lazy singleton ───────────────────────────────────────────────

_tracer: GraphTracer | None = None

def get_tracer() -> GraphTracer:
    """
    Return the module-level GraphTracer singleton.
    Instantiated on first call so the graph compiles without Azure credentials.
    """
    global _tracer
    if _tracer is None:
        _tracer = GraphTracer()
    return _tracer    
                                                 


