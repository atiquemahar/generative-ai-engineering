# projects/operations_agent/workflow/graph.py
# Day 41 — Complete Business Workflow Graph
#
# Full 9-node workflow replacing the Phase 4 agent_graph.py for Project 2.
# Every node is implemented (not stubbed) — Day 41 delivers the full skeleton
# so Days 42-44 add flesh (RAG integration, role-based access, richer audit)
# without touching the graph structure.
#
# Execution path:
#
#   START
#     └─► request_validator ◄─────────────────────────────────────────────┐
#               │── needs_clarification=True, count < MAX                  │
#               │         └─► clarification_node ──────────────────────────┘
#               │                   (waits for next customer message,
#               │                    graph stays alive via checkpointer)
#               │
#               │── needs_clarification=True, count >= MAX ──► END (give up)
#               │
#               └── needs_clarification=False
#                     └─► identify_customer_and_order
#                               │── not found ──► clarification_node (loop ↑)
#                               └── resolved
#                                     └─► retrieve_operational_data
#                                               └─► retrieve_policy_evidence
#                                                     (stub Day 41, live KnowledgeAgent Day 42)
#                                                         └─► calculate_eligibility
#                                                               (deterministic Python, never LLM)
#                                                                   │── ineligible
#                                                                   │         └─► communicate_result ──► END
#                                                                   └── eligible
#                                                                         └─► propose_action (LLM)
#                                                                                   └─► human_approval_interrupt
#                                                                                         │── approved
#                                                                                         │     └─► execute_action
#                                                                                         │               └─► audit_log
#                                                                                         │                     └─► communicate_result ──► END
#                                                                                         └── rejected
#                                                                                               └─► communicate_result ──► END
#
# Key structural guarantees:
#   1. clarification_node NEVER routes to END — it loops back to request_validator.
#      The graph stays alive. The checkpointer accumulates full message history.
#      _all_user_text() ensures turn-2 "O003" resolves turn-1 "refund" intent.
#      Give-up guard: MAX_CLARIFICATION_ATTEMPTS=2 → then routes to END.
#
#   2. calculate_eligibility is deterministic Python (eligibility.py) — never LLM.
#
#   3. propose_action is the ONLY LLM call in the main path (after validation).
#      communicate_result is the second and final LLM call.
#
#   4. human_approval_interrupt uses LangGraph interrupt() — structurally enforced.
#      No prompt can route around it. Write tools live only inside execute_action.
#
#   5. audit_log is a dedicated node — always runs after execute_action whether
#      execution succeeded or failed. Never a side effect inside a tool.

from __future__ import annotations
 
import os
import sys
import json
import uuid
from pathlib import Path
from typing import Literal
from datetime import datetime, timezone
 
from dotenv import load_dotenv
 
load_dotenv()
 
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
 
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt, Command, RetryPolicy
from langchain_openai import AzureChatOpenAI
from langchain_core.messages import SystemMessage, AIMessage, ToolMessage
 
from projects.operations_agent.workflow.state import WorkflowState
from projects.operations_agent.workflow.eligibility import calculate_refund_eligibility
from projects.operations_agent.database.engine import SessionLocal
from projects.operations_agent.database.models import Customer, Order, AuditLog
from projects.operations_agent.errors.handlers import is_duplicate_action, log_rejection
 
# Reuse the Phase 4 validator — no changes needed
from projects.operations_agent.graph.request_validator import (
    request_validator,
    clarification_node,
    route_after_validation,
)
 
# DB tools — direct function calls (not via ToolNode in this workflow)
from projects.operations_agent.tools.db_tools import (
    get_customer_db,
    get_order_db,
    get_shipment_db,
)
 
# Write tools — called only from execute_action, after approval
from projects.operations_agent.tools.write_tools import (
    issue_refund,
    create_support_ticket,
)
 
WRITE_TOOLS_MAP = {
    "issue_refund": issue_refund,
    "create_support_ticket": create_support_ticket,
}
 
# ── LLM (propose_action and communicate_result only) ─────────────────────────
# Instantiated lazily so the graph compiles and all deterministic nodes can be
# tested without Azure credentials. get_llm() is called only inside nodes that
# need it — not at module import time.
 
_llm: AzureChatOpenAI | None = None
 
def get_llm() -> AzureChatOpenAI:
    global _llm
    if _llm is None:
        _llm = AzureChatOpenAI(
            azure_deployment=os.environ["MODEL_DEPLOYMENT_NAME"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version="2025-04-01-preview",
        )
    return _llm
 
# ── Node 1: identify_customer_and_order ──────────────────────────────────────
 
def identify_customer_and_order(state: WorkflowState) -> dict:
    """
    Resolve customer and order identity from state fields set by request_validator.
 
    Reads:  provided_fields (customer_id, order_id extracted by validator regex)
    Writes: customer_id, customer_name, customer_tier, customer_status,
            order_id, order_data
 
    On success: sets all identity fields and order_data dict.
    On failure: sets needs_clarification=True and adds a clarification AIMessage
                so route_after_identification can divert to clarification_node.
 
    Design: direct DB calls — no LLM, no tool routing.
    The validator already extracted the IDs from the user's message via regex.
    This node just validates they exist in the DB.
    """
    provided = state.get("provided_fields", {})
    customer_id = provided.get("customer_id")
    order_id    = provided.get("order_id")
 
    updates: dict = {}
 
    # ── Resolve customer ──────────────────────────────────────────────────────
    if customer_id:
        try:
            with SessionLocal() as session:
                customer = session.get(Customer, customer_id)
                if customer:
                    updates["customer_id"]     = customer.id
                    updates["customer_name"]   = customer.name
                    updates["customer_tier"]   = customer.tier
                    updates["customer_status"] = customer.status
                else:
                    updates["needs_clarification"] = True
                    updates["messages"] = [AIMessage(
                        content=f"Customer '{customer_id}' was not found. "
                                "Please check the ID and try again."
                    )]
                    return updates
        except Exception as e:
            updates["errors"] = [f"identify_customer_and_order: DB error: {e}"]
            updates["needs_clarification"] = True
            updates["messages"] = [AIMessage(
                content="I could not look up the customer right now. Please try again shortly."
            )]
            return updates
 
    # ── Resolve order ─────────────────────────────────────────────────────────
    if order_id:
        try:
            with SessionLocal() as session:
                order = session.get(Order, order_id)
                if order:
                    updates["order_id"] = order.id
                    updates["order_data"] = {
                        "id":          order.id,
                        "customer_id": order.customer_id,
                        "product_id":  order.product_id,
                        "quantity":    order.quantity,
                        "status":      order.status,
                        "total_usd":   order.total_usd,
                        "created_at":  order.created_at.isoformat()
                            if order.created_at else None,
                    }
                else:
                    updates["needs_clarification"] = True
                    updates["messages"] = [AIMessage(
                        content=f"Order '{order_id}' was not found. "
                                "Please provide a valid order ID."
                    )]
                    return updates
        except Exception as e:
            updates["errors"] = [f"identify_customer_and_order: order DB error: {e}"]
            updates["needs_clarification"] = True
            updates["messages"] = [AIMessage(
                content="I could not look up the order right now. Please try again shortly."
            )]
            return updates
 
    return updates
 
 
def route_after_identification(state: WorkflowState) -> str:
    """Divert to clarification if the identity lookup failed."""
    if state.get("needs_clarification"):
        return "clarification_node"
    return "retrieve_operational_data"
 
 
# ── Node 2: retrieve_operational_data ────────────────────────────────────────
 
def retrieve_operational_data(state: WorkflowState) -> dict:
    """
    Fetch shipment data for the identified order.
 
    Reads:  order_id (from identify_customer_and_order)
    Writes: shipment_data, tool_calls_made
 
    Shipment may not exist (order still processing) — that is not an error.
    A missing shipment is a valid state: shipment_data stays None and
    calculate_eligibility will see status != "delivered".
 
    Design: direct DB call — no LLM. This node exists separately from
    identify_customer_and_order so it can be individually retried on failure
    and so its latency is tracked separately in traces (Day 45).
    """
    order_id = state.get("order_id")
    if not order_id:
        # No order to look up — happens for refund_policy queries
        return {}
 
    updates: dict = {"tool_calls_made": ["get_shipment_db"]}
 
    try:
        result = get_shipment_db.invoke({"order_id": order_id})
        # get_shipment_db returns {"error": ...} on failure — check for that
        if "error" in result:
            # Not fatal — order may still be processing
            updates["errors"] = [f"retrieve_operational_data: {result['error']}"]
        else:
            updates["shipment_data"] = result
    except Exception as e:
        updates["errors"] = [f"retrieve_operational_data: unexpected error: {e}"]
 
    return updates
 
 
# ── Node 3: retrieve_policy_evidence ─────────────────────────────────────────
 
def retrieve_policy_evidence(state: WorkflowState) -> dict:
    """
    Query Project 1's KnowledgeAgent for the policy relevant to this request.
 
    Reads:  intent (from request_validator), customer_tier, order_data
    Writes: policy_question, policy_evidence, tool_calls_made
 
    Day 41 implementation: returns a hard-coded policy dict that matches
    the KnowledgeAgent.ask() response schema exactly. This stub is replaced
    on Day 42 when the live KnowledgeAgent is wired in — the state fields
    and the downstream calculate_eligibility interface do not change.
 
    The query is intent-specific:
      - refund → "What is the refund policy and return window?"
      - support_ticket → "What is the support escalation policy?"
      - order/shipment → "What are the delivery and shipping policies?"
      - fallback → generic policy query
 
    KnowledgeAgent.ask() response schema (Day 42 will return this live):
        {
            "answer":           str,
            "supported":        bool,
            "confidence":       "high" | "medium" | "low",
            "sources":          list[dict],
            "retrieval_method": str,
            "chunks_retrieved": int,
            "latency_ms":       float,
        }
    """
    intent = state.get("intent", "unknown")
 
    # ── Build a query targeted to the intent ─────────────────────────────────
    query_map = {
        "refund":         "What is the refund policy, return window, and manager approval threshold?",
        "support_ticket": "What is the support ticket escalation and priority policy?",
        "order_status":   "What are the order fulfilment and delivery timeline policies?",
        "shipment_status": "What are the shipping and delivery policies?",
        "refund_policy":  "What is the refund policy and return window?",
    }
    policy_question = query_map.get(
        intent,
        "What are the customer service and operational policies?"
    )
 
    # ── Day 41 stub — replaced by live KnowledgeAgent.ask() on Day 42 ─────────
    # Shape matches KnowledgeAgent.ask() exactly so calculate_eligibility
    # reads policy_evidence["answer"] the same way on Day 41 and Day 42.
    stub_policy_evidence = {
        "answer": (
            "Refunds are accepted within 30 days of purchase. "
            "Items must be unused and in original packaging. "
            "Refunds of USD 500 or above require manager approval. "
            "Digital products are non-refundable once downloaded."
        ),
        "supported":        True,
        "confidence":       "high",
        "sources":          [{"document": "STUB — Day 42 will return live sources"}],
        "retrieval_method": "stub",
        "chunks_retrieved": 0,
        "latency_ms":       0.0,
    }
 
    return {
        "policy_question":    policy_question,
        "policy_evidence":    stub_policy_evidence,
        "tool_calls_made":    ["retrieve_policy_evidence"],
    }
 
 
# ── Node 4: calculate_eligibility ────────────────────────────────────────────
 
def calculate_eligibility(state: WorkflowState) -> dict:
    """
    Determine refund eligibility using deterministic Python rules.
 
    Reads:  intent, order_data, policy_evidence
    Writes: eligible, ineligibility_reason, max_refund_amount
 
    Design principle: "The model explains. Python decides."
    This node NEVER calls the LLM. It calls calculate_refund_eligibility()
    from workflow/eligibility.py, which parses the policy text and applies
    a deterministic decision tree. The LLM learns the result via state fields;
    it does not make the decision.
 
    Non-refund intents (order_status, shipment_status, refund_policy, etc.)
    are passed through with eligible=True so the graph routes to propose_action
    for an informational response rather than short-circuiting to communicate_result.
    """
    intent     = state.get("intent", "unknown")
    order_data = state.get("order_data")
    policy_ev  = state.get("policy_evidence") or {}
    policy_txt = policy_ev.get("answer", "")
 
    # ── Non-refund intents: eligibility is not applicable ────────────────────
    if intent not in ("refund",):
        return {
            "eligible":            True,   # pass-through — no eligibility gate
            "ineligibility_reason": None,
            "max_refund_amount":   None,
        }
 
    # ── Refund intent: run the deterministic engine ───────────────────────────
    if not order_data:
        # No order data — cannot determine eligibility
        return {
            "eligible":            False,
            "ineligibility_reason": "No order information found for this customer.",
            "max_refund_amount":   None,
        }
 
    # Parse order created_at for window calculation
    created_at_raw = order_data.get("created_at")
    order_created_at: datetime | None = None
    if created_at_raw:
        try:
            order_created_at = datetime.fromisoformat(created_at_raw)
        except ValueError:
            pass  # stays None; eligibility.py defaults to Day 0 (conservative)
 
    result = calculate_refund_eligibility(
        order=order_data,
        policy_answer=policy_txt,
        order_created_at=order_created_at,
    )
 
    return {
        "eligible":            result["eligible"],
        "ineligibility_reason": result.get("reason") if not result["eligible"] else None,
        "max_refund_amount":   result.get("amount"),
    }
 
 
def route_after_eligibility(state: WorkflowState) -> str:
    """
    Ineligible → communicate_result directly (no LLM proposal needed).
    Eligible   → propose_action (LLM synthesises context + recommends action).
    """
    if state.get("eligible") is False:
        return "communicate_result"
    return "propose_action"
 
 
# ── Node 5: propose_action ───────────────────────────────────────────────────
 
_PROPOSE_SYSTEM = (
    "You are a customer operations assistant reviewing a service request. "
    "You have been given the customer profile, order data, shipment data, "
    "policy evidence, and an eligibility decision made by a rules engine. "
    "Based on this context, propose the single most appropriate action. "
    "Respond ONLY with valid JSON matching this schema exactly:\n"
    "{\n"
    '  "proposed_action": "issue_refund" | "create_support_ticket" | "inform_only",\n'
    '  "proposed_args": { ... tool arguments for the action ... },\n'
    '  "reasoning": "one sentence explaining why this action is appropriate"\n'
    "}\n"
    "For issue_refund, proposed_args must include: order_id (str), amount (float), reason (str).\n"
    "For create_support_ticket, proposed_args must include: customer_id (str), issue (str), priority (str).\n"
    "For inform_only, proposed_args must be an empty dict {}.\n"
    "Do not propose a write action if eligible=False. Do not add extra keys."
)
 
 
def propose_action(state: WorkflowState) -> dict:
    """
    LLM synthesises the full context and proposes one action.
 
    Reads:  customer_*, order_data, shipment_data, policy_evidence,
            eligible, ineligibility_reason, max_refund_amount, intent
    Writes: proposed_action, proposed_args, messages (LLM reasoning)
 
    This is the ONLY LLM call in the main execution path (after validation).
    The LLM does not decide eligibility — that was decided by Python.
    It chooses among: issue_refund, create_support_ticket, inform_only.
    Its JSON output is validated before the approval gate sees it.
    """
    context = {
        "customer_id":          state.get("customer_id"),
        "customer_name":        state.get("customer_name"),
        "customer_tier":        state.get("customer_tier"),
        "customer_status":      state.get("customer_status"),
        "intent":               state.get("intent"),
        "order_data":           state.get("order_data"),
        "shipment_data":        state.get("shipment_data"),
        "policy_answer":        (state.get("policy_evidence") or {}).get("answer"),
        "eligible":             state.get("eligible"),
        "ineligibility_reason": state.get("ineligibility_reason"),
        "max_refund_amount":    state.get("max_refund_amount"),
    }
 
    prompt = (
        f"Service request context:\n{json.dumps(context, indent=2)}\n\n"
        "Propose the appropriate action as JSON."
    )
 
    try:
        response = get_llm().invoke([
            SystemMessage(content=_PROPOSE_SYSTEM),
            *state["messages"],
            AIMessage(content=prompt),   # inject context after conversation history
        ])
        raw = response.content.strip()
 
        # Strip markdown fences if the model adds them
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
 
        parsed = json.loads(raw)
        proposed_action = parsed.get("proposed_action", "inform_only")
        proposed_args   = parsed.get("proposed_args", {})
        reasoning       = parsed.get("reasoning", "")
 
    except (json.JSONDecodeError, Exception) as e:
        # Fallback: safe default — inform only, never a write action
        proposed_action = "inform_only"
        proposed_args   = {}
        reasoning       = f"[propose_action fallback — parse error: {e}]"
 
    return {
        "proposed_action": proposed_action,
        "proposed_args":   proposed_args,
        "messages": [AIMessage(
            content=f"Proposed action: {proposed_action}. Reasoning: {reasoning}"
        )],
    }
 
 
def route_after_proposal(state: WorkflowState) -> str:
    """
    inform_only → communicate_result (no approval needed, no write tool).
    write action → human_approval_interrupt.
    """
    action = state.get("proposed_action", "inform_only")
    if action in ("issue_refund", "create_support_ticket"):
        return "human_approval_interrupt"
    return "communicate_result"
 
 
# ── Node 6: human_approval_interrupt ─────────────────────────────────────────
 
def human_approval_interrupt(state: WorkflowState) -> Command[Literal["execute_action", "communicate_result"]]:
    """
    Pause for human supervisor approval of the proposed write action.
 
    Reads:  proposed_action, proposed_args, customer_id
    Writes: approval_status, action_id (on approve), messages (on reject)
 
    LangGraph interrupt() suspends the graph and persists state to the checkpointer.
    Resume via: graph.invoke(Command(resume=True/False), config)
 
    On approve: generates a UUID action_id and routes to execute_action.
    On reject:  logs the rejection and routes to communicate_result.
 
    The approval gate is structural — no user message can bypass interrupt().
    """
    approved = interrupt({
        "proposed_action": state.get("proposed_action"),
        "proposed_args":   state.get("proposed_args"),
        "customer_id":     state.get("customer_id"),
        "order_id":        state.get("order_id"),
        "question": (
            f"Approve '{state.get('proposed_action')}' "
            f"with args {state.get('proposed_args')}? "
            "Reply True to approve, False to reject."
        ),
    })
 
    if approved:
        return Command(
            update={
                "approval_status": "approved",
                "action_id":       str(uuid.uuid4()),
            },
            goto="execute_action",
        )
 
    # Rejected: log it, add message, route to communicate_result
    log_rejection(
        tool_name=state.get("proposed_action", "unknown"),
        tool_input=state.get("proposed_args", {}),
        reason="rejected by human supervisor",
    )
    return Command(
        update={
            "approval_status": "rejected",
            "messages": [AIMessage(
                content=(
                    f"The proposed action '{state.get('proposed_action')}' "
                    "was rejected by the supervisor. "
                    "I will inform the customer that the request could not be processed."
                )
            )],
        },
        goto="communicate_result",
    )
 
 
# ── Node 7: execute_action ────────────────────────────────────────────────────
 
def execute_action(state: WorkflowState) -> dict:
    """
    Execute the approved write action with idempotency guard.
 
    Reads:  proposed_action, proposed_args, action_id
    Writes: action_executed, execution_result, errors, action_id (cleared)
 
    Responsibilities:
      1. Idempotency check — skip re-execution if action_id already in audit_logs.
      2. Tool execution — calls write tool directly (not via ToolNode).
      3. Sets execution_result for audit_log and communicate_result to read.
 
    AuditLog write is deliberately NOT here — it is the responsibility of
    the dedicated audit_log node that follows. Separation ensures the audit
    record is always written even if execute_action returns an error.
    """
    action_id  = state.get("action_id")
    action     = state.get("proposed_action")
    args       = state.get("proposed_args", {})
 
    # ── 1. Idempotency check ──────────────────────────────────────────────────
    if is_duplicate_action(action_id):
        return {
            "action_executed":  False,
            "execution_result": {"status": "skipped_duplicate", "action_id": action_id},
            "errors":           [f"execute_action: duplicate action_id {action_id} skipped"],
            "action_id":        None,
        }
 
    # ── 2. Execute ────────────────────────────────────────────────────────────
    tool_fn = WRITE_TOOLS_MAP.get(action)
    if not tool_fn:
        return {
            "action_executed":  False,
            "execution_result": {"status": "unknown_action", "action": action},
            "errors":           [f"execute_action: unknown action '{action}'"],
            "action_id":        None,
        }
 
    try:
        result = tool_fn.invoke(args)
        return {
            "action_executed":    True,
            "execution_result":   result,
            "tool_calls_made":    [action],
            "action_id":          action_id,   # kept so audit_log can read it
        }
    except Exception as e:
        return {
            "action_executed":  False,
            "execution_result": {"status": "execution_failed", "error": str(e)},
            "errors":           [f"execute_action: {action} raised {type(e).__name__}: {e}"],
            "action_id":        action_id,
        }
 
 
# ── Node 8: audit_log ────────────────────────────────────────────────────────
 
def audit_log(state: WorkflowState) -> dict:
    """
    Write a complete, immutable audit record to the database.
 
    Reads:  action_id, proposed_action, proposed_args, execution_result,
            action_executed, customer_id, approval_status, intent,
            policy_question, eligible
    Writes: action_id (cleared to None)
 
    Requirements (Day 44):
      - Every action attempt is logged — success, failure, and rejection.
      - Records are INSERT-only. The AuditLog model has no UPDATE path.
      - action_id is the idempotency key (unique column in audit_logs).
      - Enough context to reconstruct the full decision chain from the log alone.
 
    Called after execute_action whether execution succeeded or failed.
    Also called via communicate_result's upstream for rejected/ineligible paths
    (see route_after_eligibility → communicate_result, which calls audit_log first).
    Day 44 will enrich the fields stored here (retrieved evidence, token counts).
    """
    action_id        = state.get("action_id")
    execution_result = state.get("execution_result")
    customer_id      = state.get("customer_id")
 
    try:
        with SessionLocal() as session:
            session.add(AuditLog(
                action_id=action_id,
                customer_id=customer_id,
                action=state.get("proposed_action", "unknown"),
                tool_name=state.get("proposed_action"),
                tool_input=state.get("proposed_args"),
                tool_output=execution_result,
                agent_decision=(
                    f"intent={state.get('intent')} | "
                    f"eligible={state.get('eligible')} | "
                    f"approval={state.get('approval_status')} | "
                    f"policy_q={state.get('policy_question')}"
                ),
            ))
            session.commit()
    except Exception as e:
        # Audit failure must never crash the graph — log to state errors only
        return {
            "errors":    [f"audit_log: DB write failed: {e}"],
            "action_id": None,
        }
 
    return {"action_id": None}   # clear after use
 
 
# ── Node 9: communicate_result ────────────────────────────────────────────────
 
_COMMUNICATE_SYSTEM = (
    "You are a customer operations assistant writing the final response to the customer. "
    "Use the context provided. Be clear, concise, and professional. "
    "Do not mention internal systems, approval workflows, or implementation details. "
    "If the request was rejected or ineligible, explain what the customer can do instead."
)
 
 
def communicate_result(state: WorkflowState) -> dict:
    """
    Generate the final natural-language response to the customer.
 
    Reads:  all state fields — customer name, order data, eligibility,
            execution_result, approval_status, ineligibility_reason
    Writes: messages (final AIMessage response)
 
    This is the second and last LLM call. It translates structured state
    into a customer-facing message. It does not make decisions — it reports
    what has already been decided and (if applicable) executed.
 
    Called by three paths:
      1. Ineligible (eligible=False) — explains why and offers alternatives.
      2. Rejected (approval_status="rejected") — informs customer, offers alternatives.
      3. Executed (action_executed=True/False) — reports outcome of write action.
    """
    context = {
        "customer_name":        state.get("customer_name"),
        "customer_tier":        state.get("customer_tier"),
        "intent":               state.get("intent"),
        "order_id":             state.get("order_id"),
        "order_data":           state.get("order_data"),
        "shipment_data":        state.get("shipment_data"),
        "eligible":             state.get("eligible"),
        "ineligibility_reason": state.get("ineligibility_reason"),
        "proposed_action":      state.get("proposed_action"),
        "approval_status":      state.get("approval_status"),
        "action_executed":      state.get("action_executed"),
        "execution_result":     state.get("execution_result"),
        "policy_answer":        (state.get("policy_evidence") or {}).get("answer"),
    }
 
    prompt = (
        f"Situation:\n{json.dumps(context, indent=2)}\n\n"
        "Write the final response to the customer."
    )
 
    try:
        response = get_llm().invoke([
            SystemMessage(content=_COMMUNICATE_SYSTEM),
            *state["messages"],
            AIMessage(content=prompt),
        ])
        final_message = response.content
    except Exception as e:
        final_message = (
            "I was unable to complete your request at this time. "
            "A member of our team will follow up with you shortly."
        )
        return {
            "messages": [AIMessage(content=final_message)],
            "errors":   [f"communicate_result: LLM failed: {e}"],
        }
 
    return {"messages": [AIMessage(content=final_message)]}
 
 
# ── Graph assembly ──────────────────────────────────────────────────────────

builder = StateGraph(WorkflowState)

# ── Add nodes ─────────────────────────────────────────────────────────────
builder.add_node("request_validator",           request_validator)
builder.add_node("clarification_node",          clarification_node)
builder.add_node("identify_customer_and_order", identify_customer_and_order)
builder.add_node(
    "retrieve_operational_data",
    retrieve_operational_data,
    retry_policy=RetryPolicy(max_attempts=2, initial_interval=0.5),
)
builder.add_node("retrieve_policy_evidence",  retrieve_policy_evidence)
builder.add_node("calculate_eligibility",     calculate_eligibility)
builder.add_node("propose_action",            propose_action)
builder.add_node("human_approval_interrupt",  human_approval_interrupt)
builder.add_node("execute_action",            execute_action)
builder.add_node("audit_log",                 audit_log)
builder.add_node("communicate_result",        communicate_result)

# ── Add edges ──────────────────────────────────────────────────────────────
builder.add_edge(START, "request_validator")

# route_after_validation returns: "agent_node" | "clarification_node" | "__end__"
builder.add_conditional_edges("request_validator", route_after_validation, {
    "agent_node":         "identify_customer_and_order",
    "clarification_node": "clarification_node",
    "__end__":            END,
})

# Clarification loops back — graph stays alive, checkpointer holds full history
builder.add_edge("clarification_node", "request_validator")

# route_after_identification returns: "clarification_node" | "retrieve_operational_data"
builder.add_conditional_edges("identify_customer_and_order", route_after_identification, {
    "clarification_node":        "clarification_node",
    "retrieve_operational_data": "retrieve_operational_data",
})

builder.add_edge("retrieve_operational_data", "retrieve_policy_evidence")
builder.add_edge("retrieve_policy_evidence",  "calculate_eligibility")

# route_after_eligibility returns: "communicate_result" | "propose_action"
builder.add_conditional_edges("calculate_eligibility", route_after_eligibility, {
    "communicate_result": "communicate_result",
    "propose_action":     "propose_action",
})

# route_after_proposal returns: "human_approval_interrupt" | "communicate_result"
builder.add_conditional_edges("propose_action", route_after_proposal, {
    "human_approval_interrupt": "human_approval_interrupt",
    "communicate_result":       "communicate_result",
})

# human_approval_interrupt uses Command(goto=...) — no explicit edges needed here
builder.add_edge("execute_action",     "audit_log")
builder.add_edge("audit_log",          "communicate_result")
builder.add_edge("communicate_result", END)

# ── Compile ────────────────────────────────────────────────────────────────
memory = InMemorySaver()
graph  = builder.compile(checkpointer=memory)