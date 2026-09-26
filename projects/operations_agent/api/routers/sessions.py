# projects/operations_agent/api/routers/sessions.py
# Day 46 — Dockerize
#
# REST surface for the 12-node operations workflow graph.
#
# ── Endpoint map ─────────────────────────────────────────────────────────────
#
#   POST   /sessions                        Start (or re-enter) a conversation
#   GET    /sessions/{thread_id}            Read current state without running
#   POST   /sessions/{thread_id}/messages   Send a follow-up message
#   POST   /sessions/{thread_id}/approve    Approve / reject a proposed action
#
# ── interrupt() bridge ───────────────────────────────────────────────────────
#
#   LangGraph's interrupt() raises GraphInterrupt inside graph.invoke().
#   The router catches it, reads state.next, and returns
#   status="awaiting_approval" — the caller can then POST /approve.
#
#   Completion vs interruption is detected via state.next:
#     ()              → END reached → status="complete"
#     ("human_approval_interrupt",) → waiting for approval
#     any other node  → waiting for clarification (clarification_node)
#
# ── Thread lifecycle ─────────────────────────────────────────────────────────
#
#   The graph uses InMemorySaver as checkpointer.  Each thread_id is an
#   independent conversation.  Sessions are lost on process restart — a
#   SqliteSaver or RedisSaver would persist them across restarts.

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from projects.operations_agent.workflow.graph import graph
from projects.operations_agent.api.schemas import (
    ApprovalRequest,
    ContinueSessionRequest,
    SessionResponse,
    StartSessionRequest,
)

router = APIRouter(tags=["sessions"])

# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_graph(input_: Any, config: dict) -> None:
    """
    Call graph.invoke() and swallow GraphInterrupt.
 
    Any other exception propagates so the caller can wrap it in an HTTP 500.
    GraphInterrupt is expected — it means the graph paused at interrupt().
    """
    try: 
        graph.invoke(input_, config)
    except Exception as exc:
        # GraphInterrupt is the expected pause signal; all others are real errors.
        # Check by name to avoid hard-coding the import path across LangGraph versions.
        if "interrupt" not in type(exc).__name__.lower():
            raise


def _build_response(thread_id: str) -> SessionResponse:
    """
    Read graph state and translate it to a SessionResponse.
 
    state.next is the canonical source of truth for graph position:
      ()                              → ran to END
      ("human_approval_interrupt",)  → paused at approval gate
      anything else                  → paused at clarification_node
    """
    state = graph.get_state({"configurable": {"thread_id": thread_id}})

    if not state.values():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {thread_id!r} not found.",
        )

    v = state.values()
    next_nodes = state.next      # tuple of node names that will run next

    # ── Determine status ──────────────────────────────────────────────────────
    if not next_nodes:
        session_status = "complete"
    elif "human_approval_interrupt" in next_nodes:
        session_status = "awaiting_approval"
    else:
        session_status = "awaiting_clarification"

    # ── Extract last AI message as the human-readable response ────────────────
    last_ai: str | None = None
    for msg in reversed(v.get("messages", [])):
        if hasattr(msg, "type") and msg.type == "ai":
            last_ai = str(msg.content) 
            break


    # ── Execution outcome ───────────────────────────────────────────────────── 
    exec_result = v.get("execution_result") or {}
    outcome: str | None = None
    if v.get("action_executed") is True:
        outcome = "success"
    elif exec_result.get("status") == "role_denied":
        outcome = "role_denied" 
    elif v.get("eligible") is False:
        outcome = "ineligible" 
    elif v.get("approval_status") == "rejected":
        outcome = "rejected"
    elif session_status == "complete" and v.get("proposed_action") == "inform_only":
        outcome = "inform_only"


    return SessionResponse(
        thread_id=thread_id,
        status=session_status,
        response=last_ai,
        intent=v.get("intent"),
        eligible=v.get("eligible"),
        proposed_action=v.get("proposed_action"),
        proposed_args=v.get("proposed_args"),
        execution_outcome=outcome,
        errors=v.get("errors") or None,
    ) 

# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="start a new session",
) 

def start_session(req: StartSessionRequest) -> SessionResponse:
    """
    Submit the customer's first message and run the workflow graph.
 
    Returns immediately when the graph reaches END or hits an interrupt.
    If status="awaiting_approval", use POST /sessions/{thread_id}/approve.
    If status="awaiting_clarification", use POST /sessions/{thread_id}/messages.
    """
    thread_id = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    _run_graph(
        {"messages": [HumanMessage(req.message)], "user_role": req.user_role},
        config,
    )
    return _build_response(thread_id)

@router.get(
    "/{thread_id}",
    response_model=SessionResponse,
    summary="Continue a session",
)
def continue_session(thread_id: str, req: ContinueSessionRequest) -> SessionResponse:
    """
    Send a follow-up message to a session that is awaiting_clarification.
 
    The graph picks up from where it left off (checkpointer restores state).
    The new message is appended to the conversation via add_messages reducer.
    """
    # Validate the session exists before running
    state = graph.get_state({"configurable": {"thread_id": thread_id}})
    if not state.values:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {thread_id!r} not found.",
        )

    config = {"configurable": {"thread_id": thread_id}}
    _run_graph({"messages": [HumanMessage(content=req.message)]}, config)
    return _build_response(thread_id)

@router.post(
    "/{thread_id}/approve",
    response_model=SessionResponse,
    summary="Approve or reject a proposed action",
)
def approve_action(thread_id: str, req: ApprovalRequest) -> SessionResponse:
    """
    Resume the graph after human_approval_interrupt.
 
    Pass {"approved": true} to execute the proposed write action,
    or {"approved": false} to reject it and inform the customer.
 
    Returns 409 if the session is not currently awaiting approval.
    """
    state = graph.get_state({"configurable": {"thread_id": thread_id}})
    if not state.values:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {thread_id!r} not found.",
        )

    next_nodes = state.next
    if "human_approval_interrupt" not in next_nodes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Session {thread_id!r} is not awaiting approval "
                f"(current position: {next_nodes or 'END'})."
            ),
        )

    config = {"configurable": {"thread_id": thread_id}}
    _run_graph(Command(resume=req.approved), config)
    return _build_response(thread_id)

     

