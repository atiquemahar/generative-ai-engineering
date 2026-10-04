# projects/operations_agent/workflow/roles.py
# Day 43 — Role-Based Tool Access
#
# Defines which tools each role may use and which write actions each role
# may propose. Enforced in two places:
#
#   1. propose_action (LLM node) — the system prompt lists only the actions
#      the caller's role is permitted to propose. The LLM cannot propose
#      issue_refund if the role is "support".
#
#   2. role_guard (new node, runs before execute_action) — structurally
#      blocks execution of any write action not in the caller's permission
#      set, regardless of what propose_action returned. This is the hard gate.
#
# Design: two layers, not one.
#   - Prompt-level restriction  → LLM won't propose disallowed actions
#                                  (good UX — customer never sees a refund offer)
#   - Structural guard          → even if the LLM hallucinates a disallowed
#                                  action, role_guard blocks it before execution
#
# Role hierarchy (additive):
#   customer   → read own order/shipment/policy only
#   support    → read anything + create support tickets
#   supervisor → everything including issue_refund
#
# Roles are set at session start via WorkflowState["user_role"].
# If user_role is None or unrecognised, defaults to "customer" (least privilege).

from __future__ import annotations
from typing import Literal

# ── Tool permission registry ───────────────────────────────────────────────
# Keys match the @tool name strings registered in db_tools.py / write_tools.py.
# These are used to build the allowed tool list for propose_action's system prompt
# AND to validate the proposed action in role_guard.

TOOL_PERMISSIONS: dict[str, list[str]] = {
    "customer": [
        "get_order_db",
        "get_shipment_db",
        "get_refund_policy_db",
    ],
    "support": [
        "get_order_db",
        "get_shipment_db",
        "get_refund_policy_db",
        "get_customer_db",
        "check_inventory_db",
        "create_support_ticket",
    ],
    "supervisor": [
        "get_order_db",
        "get_shipment_db",
        "get_refund_policy_db",
        "get_customer_db",
        "check_inventory_db",
        "create_support_ticket",
        "issue_refund",
    ],
}

# Write actions that require approval AND role check.
# read-only tools are not listed here — they are never gated by role_guard
# (only propose_action's prompt excludes them for lower roles).

WRITE_ACTIONS: set[str] = {"issue_refund", "create_support_ticket"}

# Default role when user_role is None or unrecognised — always least privilege
DEFAULT_ROLE: str = "customer"

def get_allowed_tools(role: str | None) -> list[str]:
    """
    Return the list of tool names permitted for this role.
    Unrecognised or None role defaults to "customer" (least privilege).
    """
    return TOOL_PERMISSIONS.get(role or DEFAULT_ROLE, TOOL_PERMISSIONS[DEFAULT_ROLE])

def is_action_permitted(role: str | None, action: str) -> bool:
    """
        Return True if the role is allowed to execute this action.
    
        Day 49 change — enforces ALL tools, not just write actions:
          Before: read-only tools always returned True (structural gap).
          After:  every tool is checked against TOOL_PERMISSIONS[role].
    
        This means:
          - customer calling get_customer_db → False  (data exfiltration blocked)
          - support calling issue_refund     → False  (write gate unchanged)
          - supervisor calling anything      → True   (full access unchanged)
    
        The role_guard node calls this for every proposed action.
        If it returns False, the node routes to an error/refusal state instead
        of execute_action — the action is never executed regardless of LLM output.
    """
    resolved_role = role if role in TOOL_PERMISSIONS else DEFAULT_ROLE
    return action in TOOL_PERMISSIONS[resolved_role]

def role_description(role: str | None) -> str:
    """
    Human-readable description of what a role can do.
    Injected into propose_action's system prompt so the LLM
    knows not to propose disallowed actions.
    """
    role = role or DEFAULT_ROLE
    allowed = get_allowed_tools(role)
    write_allowed = [a for a in allowed if a in WRITE_ACTIONS]
    read_allowed = [a for a in allowed if a not in WRITE_ACTIONS] 

    lines = [f"Role: {role}"]
    lines.append(f"Read tools available: {', '.join(read_allowed) or 'none'}")
    if write_allowed:
        lines.append(f"write actions available (require approval): {', '.join(write_allowed)}")
    else:
        lines.append("write actions: NONE - you may only propose 'inform only' for this role") 
    return "\n".join(lines)       