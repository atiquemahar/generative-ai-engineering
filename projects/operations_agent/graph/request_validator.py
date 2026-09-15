#projects/operations_agent/graph/request_validator.py
from pydantic import BaseModel
import re
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage

from projects.operations_agent.state import AgentState


class RequestIntent(BaseModel):
    intent: Literal[
        "order_status",
        "shipment_status",
        "inventory_status",
        "customer_lookup",
        "refund",
        "support_ticket",
        "refund_policy",
        "unknown",
    ]
    required_fields: list[str]
    provided_fields: dict[str, str]
    needs_clarification: bool


# this focused on identity/completeness required before any lookup.
# Do not require refund amount/reason here, otherwise the validator will
# block approval-bypass scenarios before they reach the approval gate.
REQUIRED_FIELDS = {
    "order_status": ["order_id"],
    "shipment_status": ["order_id"],
    "inventory_status": ["product_id"],
    "customer_lookup": ["customer_id"],
    "refund": ["order_id"],
    "support_ticket": ["customer_id"],
    "refund_policy": [],
    "unknown": [],
}


def _latest_user_text(state: AgentState) -> str:
    for message in reversed(state["messages"]):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""

def _all_user_text(state: AgentState) -> str:
    """
    Concatenate all human messages in the session.
 
    Used during a clarification loop so that when the customer replies
    "order O003" on turn 2, the validator sees both "I want a refund"
    (turn 1) and "order O003" (turn 2) together. Without this, turn 2
    would classify as intent=unknown and re-ask for clarification forever.
    """
    parts = [
        str(m.content)
        for m in state["messages"]
        if isinstance(m, HumanMessage)
    ]
    return " ".join(parts)

def _extract_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}

    order_match = re.search(r"\bO\d+\b", text, re.IGNORECASE)
    customer_match = re.search(r"\bC\d+\b", text, re.IGNORECASE)
    product_match = re.search(r"\bP\d+\b", text, re.IGNORECASE)
    amount_match = re.search(r"\$(-?\d+(?:\.\d{1,2})?)", text)

    if order_match:
        fields["order_id"] = order_match.group(0).upper()
    if customer_match:
        fields["customer_id"] = customer_match.group(0).upper()
    if product_match:
        fields["product_id"] = product_match.group(0).upper()
    if amount_match:
        fields["amount"] = amount_match.group(1)

    return fields


def _classify_intent(text: str) -> str:
    text = text.lower()

    if "refund policy" in text:
        return "refund_policy"

    if "refund" in text or "return" in text:
        return "refund"

    if (
        "support ticket" in text
        or "create a ticket" in text
        or ("ticket" in text and ("open" in text or "create" in text))
    ):
        return "support_ticket"

    if (
        "shipment" in text
        or "track" in text
        or "where is my package" in text
        or "where's my package" in text
    ):
        return "shipment_status"

    if "stock" in text or "inventory" in text or "available" in text:
        return "inventory_status"

    if (
        "order status" in text
        or "status of order" in text
        or ("has order" in text and "delivered" in text)
    ):
        return "order_status"

    if (
        "account details" in text
        or "account information" in text
        or "look up account" in text
        or "show me account" in text
        or "look up customer" in text
        or "customer details" in text
    ):
        return "customer_lookup"

    return "unknown"


def request_validator(state: AgentState) -> dict:
    # ── Text to classify ──────────────────────────────────────────────────
    # On the first turn, classify from the latest message only.
    # On subsequent turns (clarification_count > 0), scan the full history
    # so a reply like "order O003" is understood in context of the prior
    # "I want a refund" message — without this, turn 2 always returns
    # intent=unknown and the clarification loop never terminates.
    in_clarification_loop = bool(state.get("clarification_count", 0))
    text = _all_user_text(state) if in_clarification_loop else _latest_user_text(state)
    
    intent = _classify_intent(text)
    provided_fields = _extract_fields(text)
    required_fields = REQUIRED_FIELDS[intent]

    missing_fields = [
        field for field in required_fields
        if field not in provided_fields
    ]

    needs_clarification = intent == "unknown" or bool(missing_fields)
 
    # Reset clarification_count when a complete, valid request is received
    clarification_count = state.get("clarification_count", 0)
    if not needs_clarification:
        clarification_count = 0

    result = RequestIntent(
        intent=intent,
        required_fields=required_fields,
        provided_fields=provided_fields,
        needs_clarification=(intent == "unknown" or bool(missing_fields)),
    )

    return {**result.model_dump(), "clarification_count": clarification_count}


def clarification_node(state: AgentState) -> dict:
    intent = state["intent"]
    required_fields = state["required_fields"]
    provided_fields = state["provided_fields"]
    clarification_count = state.get("clarification_count", 0)

    missing_fields = [
        field for field in required_fields
        if field not in provided_fields
    ]

    if intent == "unknown":
        message = (
            "Could you clarify what you need help with? For example, I can "
            "check an order or shipment, look up an account, check inventory, "
            "process a refund, or create a support ticket."
        )
    else:
        labels = {
            "order_id": "order ID",
            "customer_id": "customer ID",
            "product_id": "product ID",
        }
        requested = ", ".join(labels.get(field, field) for field in missing_fields)
        message = f"Please provide the {requested} so I can help."

    return {
        "messages": [AIMessage(content=message)],
        "clarification_count": clarification_count + 1,
    }

MAX_CLARIFICATION_ATTEMPTS = 2
def route_after_validation(state: AgentState) -> str:
    if not state["needs_clarification"]:
        return "agent_node"                          # ← valid request
    if state.get("clarification_count", 0) >= MAX_CLARIFICATION_ATTEMPTS:
        return "__end__"                             # ← give up (string, not END object)
    return "clarification_node"                      # ← ask again  