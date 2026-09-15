# projects/operations_agent/workflow/eligibility.py
# Day 41 — Deterministic refund eligibility engine
#
# Design principle (from roadmap Day 42):
#   "The model explains. Python decides."
#
# This module is called by calculate_eligibility_node — never by the LLM.
# All inputs are structured data from the DB tools and parsed policy text.
# No embeddings, no LLM calls, no fuzzy matching.

from __future__ import annotations

from datetime import datetime, timezone


# ── Policy constants ──────────────────────────────────────────────────────────
# These are parsed from policy text retrieved by the KnowledgeAgent.
# Hard-coded here as defaults; parse_policy_limits() overrides them when
# the knowledge agent returns a supported, high-confidence answer.
#
# Source: data/enterprise_docs/expense_guidelines/ and refund policy documents.
REFUND_WINDOW_DAYS   = 30      # "Refunds are accepted within 30 days of purchase"
MANAGER_APPROVAL_USD = 500.0   # "Refunds of USD 500 or above require manager approval"


# ── Policy parser ─────────────────────────────────────────────────────────────

def parse_policy_limits(policy_answer: str) -> dict:
    """
    Extract numeric limits from the policy answer text.

    The KnowledgeAgent returns free-text answers. This function parses
    the two numbers we need: the return window in days and the manager-
    approval threshold in USD.

    Returns dict with keys:
        max_days   (int)   — default REFUND_WINDOW_DAYS if not found
        manager_threshold (float) — default MANAGER_APPROVAL_USD if not found

    Parsing strategy: look for the first integer before "day" and the first
    float/int after "$" or "USD". Conservative — if parsing fails, use defaults
    so eligibility degrades gracefully rather than crashing.
    """
    import re

    limits = {
        "max_days":           REFUND_WINDOW_DAYS,
        "manager_threshold":  MANAGER_APPROVAL_USD,
    }

    # "within 30 days" / "30-day" / "30 days"
    day_match = re.search(r"(\d+)\s*[-–]?\s*day", policy_answer, re.IGNORECASE)
    if day_match:
        limits["max_days"] = int(day_match.group(1))

    # "USD 500" / "$500" / "$ 500.00"
    usd_match = re.search(r"(?:USD|\$)\s*(\d+(?:\.\d{1,2})?)", policy_answer, re.IGNORECASE)
    if usd_match:
        limits["manager_threshold"] = float(usd_match.group(1))

    return limits


# ── Core eligibility function ─────────────────────────────────────────────────

def calculate_refund_eligibility(
    order: dict,
    policy_answer: str,
    order_created_at: datetime | None = None,
) -> dict:
    """
    Determine refund eligibility deterministically.

    Args:
        order           — DB row dict from get_order_db
                          keys: id, customer_id, product_id, status, total_usd
        policy_answer   — text answer from KnowledgeAgent.ask() ["answer"] field
        order_created_at — datetime the order was placed (for window check).
                           Uses now() as fallback when None (fails open → Day 0).

    Returns:
        {
            "eligible":          bool,
            "reason":            str,    # always present; explains the decision
            "amount":            float | None,  # refund amount when eligible
            "requires_approval": bool,   # True when amount >= manager_threshold
            "max_days":          int,    # parsed from policy
            "days_since_order":  int,    # computed, for audit
        }

    Decision tree (checked in order — first failure wins):
        1. Order status must be "delivered" (not "processing" or "shipped")
        2. days_since_order must be <= max_days
        3. If both pass → eligible; set requires_approval if amount >= threshold
    """
    limits = parse_policy_limits(policy_answer)
    max_days          = limits["max_days"]
    manager_threshold = limits["manager_threshold"]

    # Days since order
    now = datetime.now(timezone.utc)
    if order_created_at is not None:
        # Normalise: make both tz-aware
        if order_created_at.tzinfo is None:
            order_created_at = order_created_at.replace(tzinfo=timezone.utc)
        days_since_order = (now - order_created_at).days
    else:
        days_since_order = 0  # conservative — treat as Day 0 when unknown

    # ── Check 1: order must be delivered ─────────────────────────────────
    order_status = order.get("status", "")
    if order_status != "delivered":
        return {
            "eligible":          False,
            "reason":            f"Order is not yet delivered (status: '{order_status}'). "
                                 f"Refunds can only be issued after delivery.",
            "amount":            None,
            "requires_approval": False,
            "max_days":          max_days,
            "days_since_order":  days_since_order,
        }

    # ── Check 2: within return window ────────────────────────────────────
    if days_since_order > max_days:
        return {
            "eligible":          False,
            "reason":            f"Outside the {max_days}-day return window "
                                 f"({days_since_order} days since order).",
            "amount":            None,
            "requires_approval": False,
            "max_days":          max_days,
            "days_since_order":  days_since_order,
        }

    # ── Eligible ──────────────────────────────────────────────────────────
    amount = float(order.get("total_usd", 0.0))
    requires_approval = amount >= manager_threshold

    reason = (
        f"Order delivered and within the {max_days}-day return window "
        f"({days_since_order} days since order)."
    )
    if requires_approval:
        reason += (
            f" Refund of ${amount:.2f} meets or exceeds the "
            f"${manager_threshold:.0f} manager-approval threshold."
        )

    return {
        "eligible":          True,
        "reason":            reason,
        "amount":            amount,
        "requires_approval": requires_approval,
        "max_days":          max_days,
        "days_since_order":  days_since_order,
    }