# projects/operations_agent/api/schemas.py
# Day 46 — Dockerize
#
# All Pydantic v2 models for the Operations Agent REST API.
# One file: request bodies and response envelopes in the same place
# so the router files stay free of model definitions.

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field

# ── Requests ──────────────────────────────────────────────────────────────────

class StartSessionRequest(BaseModel):
    """POST /sessions — begin a new conversation."""
    message: str = Field(..., description="Customer's first message")
    user_role: str = Field(
        "customer",
        description="Caller's role: 'customer' | 'support' | 'supervisor'",
    )
    thread_id: Optional[str] = Field(
        None,
        description="Reuse an existing thread, or omit to get a new UUID",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "message": "I want a refund for order O003 for customer C001.",
                "user_role": "supervisor",
            }
        }
    }

class ContinueSessionRequest(BaseModel):
    """POST /sessions/{thread_id}/messages — send a follow-up message."""
    message: str = Field(..., description="Customer's follow-up message")

    model_config = {
        "json_schema_extra": {
            "example": {"message": "My customer ID is C001 and order is O003."}
        }
    }

class ApprovalRequest(BaseModel):
    """POST /sessions/{thread_id}/approve — approve or reject a proposed write action."""
    approved: bool = Field(..., description="True to approve, False to reject")

    model_config = {
        "json_schema_extra": {"example": {"approved": True}}
    }

# ── Responses ─────────────────────────────────────────────────────────────────

class SessionResponse(BaseModel):
    """
    Unified envelope returned by all /sessions endpoints.
 
    status:
      "complete"              — graph ran to END; response is the final message
      "awaiting_approval"     — human_approval_interrupt fired; proposed_action
                                and proposed_args are set for the caller to review
      "awaiting_clarification"— clarification_node requested more info from user
    """
    thread_id: str
    status: str                     # complete | awaiting_approval | awaiting_clarification
    response: Optional[str] = None  # last AIMessage content (on complete)
    intent:   Optional[str] = None  # classified intent
    eligible: Optional[bool] = None # eligibility decision
    proposed_action: Optional[str] = None   # set when awaiting_approval
    proposed_args: Optional[dict] = None # set when awaiting_approval
    execution_outcome: Optional[str] = None # set on complete write-action paths
    errors: Optional[list[str]] = None # any accumulated state errors

class HealthResponse(BaseModel):
    """GET /health response."""
    status: str     # "ok" | "degraded"
    db: str         # "ok" | "error: <detail>"
    timestamp: str  # ISO 8601 UTC