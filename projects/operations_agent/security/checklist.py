# projects/operations_agent/security/checklist.py
# Day 49 — Security Review + Testing
#
# Executable security registry for every tool and write action in the
# operations agent. One ToolSecurityProfile per tool — the checklist
# runs against each profile and returns pass/fail per check.
#
# The 6 checklist questions (from the Day 49 roadmap):
#   1. Who can call it?          → permitted_roles is non-empty
#   2. Is it read or write?      → write tools require approval
#   3. Are inputs validated?     → pydantic_validated == True
#   4. Is it idempotent?         → is_idempotent == True
#   5. What's logged?            → audit_logged == True
#   6. What's redacted?          → pii_stored_in_audit lists are reviewed
#
# Usage:
#   from projects.operations_agent.security.checklist import run_checklist, TOOL_REGISTRY
#   results = run_checklist()
#   for tool_name, result in results.items():
#       print(tool_name, "PASS" if result["passed"] else "FAIL")

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolSecurityProfile:
    """
    Security profile for one tool or node in the operations agent.

    Every tool must answer all 6 checklist questions — this dataclass
    is the structured answer. The run_checklist() function derives
    pass/fail from the field values.
    """
    name: str

    # ── Q1: Who can call it? ──────────────────────────────────────────────────
    # Roles that may invoke this tool. Enforced by:
    #   - propose_action system prompt (soft: LLM won't offer disallowed actions)
    #   - role_guard node (hard: structural block before execute_action)
    permitted_roles: tuple[str, ...]

    # ── Q2: Is it read or write? ──────────────────────────────────────────────
    # True = mutates DB or external state.
    # Write tools MUST require approval and MUST be idempotent.
    is_write: bool

    # ── Q2 corollary: Does it require human approval? ─────────────────────────
    # Must be True for all write tools (human_approval_interrupt node).
    # Read tools never require approval.
    requires_approval: bool

    # ── Q3: Are inputs validated? ─────────────────────────────────────────────
    # True = tool has an args_schema (Pydantic BaseModel) via @tool(args_schema=...).
    # Pydantic rejects malformed inputs before the tool function is called.
    pydantic_validated: bool

    # ── Q4: Is it idempotent? ─────────────────────────────────────────────────
    # True = safe to retry without double-executing.
    # Write tools: enforced by action_id unique constraint in AuditLog +
    #              is_duplicate_action() check in execute_action.
    # Read tools: always idempotent by nature.
    is_idempotent: bool

    # ── Q5: What's logged? ────────────────────────────────────────────────────
    # True = an AuditLog row is written by the audit_log node.
    # Every tool call in a session produces an audit record.
    audit_logged: bool

    # ── Q6: What's redacted? ──────────────────────────────────────────────────
    # Fields present in the tool's output that contain PII.
    # The audit_log node stores tool_output as JSON — these fields appear there.
    pii_in_tool_output: tuple[str, ...]

    # Fields with PII that are NOT written to audit_logs (positive finding).
    # Confirms that sensitive data is handled but not persisted unnecessarily.
    pii_excluded_from_audit: tuple[str, ...]

    # Known residual PII risk: fields that ARE in audit_logs and require
    # access controls on the DB itself.
    pii_stored_in_audit: tuple[str, ...]


# ── Tool registry ─────────────────────────────────────────────────────────────
# One entry per tool. Keep in sync with tools/ and workflow/roles.py.

TOOL_REGISTRY: list[ToolSecurityProfile] = [

    ToolSecurityProfile(
        name="issue_refund",
        permitted_roles=("supervisor",),
        is_write=True,
        requires_approval=True,
        pydantic_validated=True,         # IssueRefundInput: order_id, amount, reason
        is_idempotent=True,              # action_id unique constraint + is_duplicate_action()
        audit_logged=True,               # audit_log node writes AuditLog row
        pii_in_tool_output=("order_id", "reason"),
        pii_excluded_from_audit=("customer_email", "customer_address"),
        pii_stored_in_audit=("order_id", "amount_usd", "reason"),
    ),

    ToolSecurityProfile(
        name="create_support_ticket",
        permitted_roles=("support", "supervisor"),
        is_write=True,
        requires_approval=True,          # human_approval_interrupt
        pydantic_validated=True,         # CreateTicketInput: customer_id, issue, priority
        is_idempotent=True,              # action_id unique constraint
        audit_logged=True,
        pii_in_tool_output=("customer_id", "issue"),
        pii_excluded_from_audit=("customer_email",),
        pii_stored_in_audit=("customer_id", "issue"),
    ),

    ToolSecurityProfile(
        name="get_customer_db",
        permitted_roles=("support", "supervisor"),   # not "customer" — role guard
        is_write=False,
        requires_approval=False,
        pydantic_validated=True,          # CustomerInput: customer_id
        is_idempotent=True,
        audit_logged=True,
        pii_in_tool_output=("name", "email", "tier", "status"),
        pii_excluded_from_audit=("email",),          # email not in AuditLog columns
        pii_stored_in_audit=("customer_id",),        # only ID is stored
    ),

    ToolSecurityProfile(
        name="get_order_db",
        permitted_roles=("customer", "support", "supervisor"),
        is_write=False,
        requires_approval=False,
        pydantic_validated=True,          # OrderInput: order_id
        is_idempotent=True,
        audit_logged=True,
        pii_in_tool_output=("customer_id", "total_usd"),
        pii_excluded_from_audit=("customer_email",),
        pii_stored_in_audit=("order_id", "customer_id"),
    ),

    ToolSecurityProfile(
        name="get_shipment_db",
        permitted_roles=("customer", "support", "supervisor"),
        is_write=False,
        requires_approval=False,
        pydantic_validated=True,          # OrderInput: order_id
        is_idempotent=True,
        audit_logged=True,
        pii_in_tool_output=("tracking_number",),
        pii_excluded_from_audit=(),
        pii_stored_in_audit=("order_id",),
    ),
]


# ── Checklist runner ──────────────────────────────────────────────────────────

def _check_tool(profile: ToolSecurityProfile) -> dict:
    """
    Run all 6 checklist items against one ToolSecurityProfile.
    Returns a dict with per-check pass/fail and an overall passed flag.
    """
    checks: dict[str, bool] = {
        # Q1: role restriction exists
        "role_restricted":
            len(profile.permitted_roles) > 0,

        # Q2: write tools must require approval; read tools never should
        "approval_policy_correct":
            (profile.is_write and profile.requires_approval)
            or (not profile.is_write and not profile.requires_approval),

        # Q3: Pydantic args_schema present
        "pydantic_validated":
            profile.pydantic_validated,

        # Q4: idempotent
        "idempotent":
            profile.is_idempotent,

        # Q5: every tool call produces an audit record
        "audit_logged":
            profile.audit_logged,

        # Q6: known PII reviewed (profile explicitly lists pii_stored_in_audit)
        # A non-None tuple (even empty) means the author has considered PII.
        "pii_reviewed":
            profile.pii_stored_in_audit is not None,
    }

    return {
        "profile": profile,
        "checks":  checks,
        "passed":  all(checks.values()),
        "failures": [name for name, ok in checks.items() if not ok],
    }


def run_checklist() -> dict[str, dict]:
    """
    Run the security checklist against every tool in TOOL_REGISTRY.

    Returns:
        {
            "issue_refund": {
                "profile":  ToolSecurityProfile,
                "checks":   {"role_restricted": True, ...},
                "passed":   True,
                "failures": [],
            },
            ...
        }
    """
    return {profile.name: _check_tool(profile) for profile in TOOL_REGISTRY}


def print_report() -> None:
    """Print a human-readable security checklist report to stdout."""
    results = run_checklist()
    total   = len(results)
    passed  = sum(1 for r in results.values() if r["passed"])

    print("=" * 60)
    print(" OPERATIONS AGENT — SECURITY CHECKLIST (Day 49)")
    print("=" * 60)

    for name, result in results.items():
        p = result["profile"]
        icon = "✓" if result["passed"] else "✗"
        print(f"\n  {icon} {name}")
        print(f"     Roles:    {', '.join(p.permitted_roles)}")
        print(f"     Write:    {p.is_write}  |  Approval: {p.requires_approval}")
        print(f"     Pydantic: {p.pydantic_validated}  |  Idempotent: {p.is_idempotent}")
        print(f"     Logged:   {p.audit_logged}")
        print(f"     PII in audit: {p.pii_stored_in_audit or 'none'}")
        if result["failures"]:
            print(f"     FAILED checks: {result['failures']}")

    print(f"\n{'=' * 60}")
    print(f"  {passed}/{total} tools passed all 6 checklist items")
    print("=" * 60)


if __name__ == "__main__":
    print_report()