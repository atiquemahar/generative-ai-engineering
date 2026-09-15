# projects/operations_agent/workflow/state.py
# Day 41 — Complete Business Workflow: finalised state schema
#
# This replaces the Phase 4 AgentState for the full workflow graph.
# Every field is owned by exactly one node — see the ownership table in
# docs/langgraph-design-decisions.md for the authoritative reference.
#
# Reducer choices:
#   add_messages  → conversation history (append + deduplicate by message ID)
#   operator.add  → tool_calls_made, errors (accumulate across all turns)
#   last-write-wins (default) → everything else

from __future__ import annotations

from typing import Annotated, Optional
from operator import add

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class WorkflowState(TypedDict):

    # ── Conversation ──────────────────────────────────────────────────────
    # Full message history. add_messages appends; never overwrites.
    # Deduplicates by message ID, so retries are safe.
    messages: Annotated[list[AnyMessage], add_messages]

    # ── Identity (set by identify_customer_and_order) ─────────────────────
    # Written once per session after the first successful DB lookup.
    # Subsequent nodes read these without re-querying.
    customer_id:   Optional[str]
    customer_name: Optional[str]
    customer_tier: Optional[str]    # "premium" | "standard" — used by eligibility
    customer_status: Optional[str]  # "active" | "suspended"
    order_id:      Optional[str]
    product_id:    Optional[str]

    # ── Operational data (set by retrieve_operational_data) ───────────────
    # Populated from DB tools. None until that node runs.
    order_data:    Optional[dict]   # {id, customer_id, product_id, status, total_usd, created_at}
    shipment_data: Optional[dict]   # {carrier, tracking_number, status, estimated_delivery}

    # ── Policy evidence (set by retrieve_policy_evidence) ─────────────────
    # The raw answer dict returned by KnowledgeAgent.ask().
    # Includes answer text, sources, confidence, supported flag.
    # calculate_eligibility reads policy_evidence["answer"] only.
    policy_question:  Optional[str]  # the query sent to KnowledgeAgent (for tracing)
    policy_evidence:  Optional[dict] # full KnowledgeAgent.ask() response dict

    # ── Eligibility (set by calculate_eligibility) ────────────────────────
    # Deterministic Python — never an LLM call.
    # eligible=None means the node has not run yet.
    eligible:         Optional[bool]
    ineligibility_reason: Optional[str]   # populated when eligible=False
    max_refund_amount:    Optional[float] # parsed from policy text; None if not a refund request

    # ── Proposed action (set by propose_action) ───────────────────────────
    # The LLM's suggested action after reviewing eligibility + data.
    proposed_action:  Optional[str]  # "issue_refund" | "create_support_ticket" | "inform_only"
    proposed_args:    Optional[dict] # tool arguments for the proposed action

    # ── Approval (managed by human_approval_interrupt) ────────────────────
    approval_required: Optional[bool]
    approval_status:   Optional[str]   # "pending" | "approved" | "rejected"
    action_id:         Optional[str]   # UUID generated on approval; consumed by execute_action

    # ── Execution (set by execute_action) ─────────────────────────────────
    action_executed:  Optional[bool]
    execution_result: Optional[dict]   # tool output dict on success

    # ── Role-based access (Day 43) ─────────────────────────────────────────
    # Set at session start; controls which tools the LLM may call.
    # Checked in role_guard (Day 43) — stub field present now.
    user_role: Optional[str]   # "customer" | "support" | "supervisor"

    # ── Audit accumulators (grow across all nodes, never reset) ──────────
    tool_calls_made: Annotated[list[str], add]
    errors:          Annotated[list[str], add]

    # ── Request classification (set by request_validator) ─────────────────
    # Carried forward from Phase 4 — same validator reused.
    intent:              Optional[str]
    required_fields:     list[str]
    provided_fields:     dict
    needs_clarification: Optional[bool]

    # ── Clarification loop guard ───────────────────────────────────────────
    # Incremented each time clarification_node runs.
    # route_after_validation routes to END (giving up) when this reaches
    # MAX_CLARIFICATION_ATTEMPTS, preventing an infinite clarification loop.
    # Reset to 0 when a complete, valid request is received.
    clarification_count: int
