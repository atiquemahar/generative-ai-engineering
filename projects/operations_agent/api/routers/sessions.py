# projects/operations_agent/api/routers/sessions.py
# Day 46 — Dockerize
#
# REST surface for the 12-node operations workflow graph.
#
# Endpoint map:
#   POST   /sessions                        Start (or re-enter) a conversation
#   GET    /sessions/{thread_id}            Read current state without running
#   POST   /sessions/{thread_id}/messages   Send a follow-up message
#   POST   /sessions/{thread_id}/approve    Approve / reject a proposed action

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


def _run_graph(input_: Any, config: dict) -> None:
    try:
        graph.invoke(input_, config)
    except Exception as exc:
        if "interrupt" not in type(exc).__name__.lower():
            raise


def _build_response(thread_id: str) -> SessionResponse:
    state = graph.get_state({"configurable": {"thread_id": thread_id}})

    if not state.values:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {thread_id!r} not found.",
        )

    v = state.values
    next_nodes = state.next

    if not next_nodes:
        session_status = "complete"
    elif "human_approval_interrupt" in next_nodes:
        session_status = "awaiting_approval"
    else:
        session_status = "awaiting_clarification"

    last_ai: str | None = None
    for msg in reversed(v.get("messages", [])):
        if hasattr(msg, "type") and msg.type == "ai":
            last_ai = str(msg.content)
            break

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


@router.post(
    "",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new session",
)
def start_session(req: StartSessionRequest) -> SessionResponse:
    thread_id = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    _run_graph(
        {"messages": [HumanMessage(req.message)], "user_role": req.user_role},
        config,
    )
    return _build_response(thread_id)


@router.post(
    "/{thread_id}/messages",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Send a follow-up message (awaiting_clarification)",
)
def continue_session(thread_id: str, req: ContinueSessionRequest) -> SessionResponse:
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

     

