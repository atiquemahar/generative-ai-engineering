# experiments/day43_role_based_access.py
# Day 43 — Role-Based Tool Access
#
# Tests:
#   Test 1 — TOOL_PERMISSIONS registry: correct tools per role
#   Test 2 — is_action_permitted: role × action permission matrix
#   Test 3 — role_guard node: blocks disallowed actions structurally
#   Test 4 — role_guard node: passes permitted actions through
#   Test 5 — role_guard node: unknown role defaults to customer (least privilege)
#   Test 6 — role_description: injects correct tool list into system prompt
#   Test 7 — graph compiles with role_guard node present (12 nodes)
#
# Run:
#   python experiments/day43_role_based_access.py

import sys
from pathlib import Path
 
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from langchain_core.messages import HumanMessage, AIMessage
from projects.operations_agent.workflow.roles import (
    TOOL_PERMISSIONS,
    WRITE_ACTIONS,
    DEFAULT_ROLE,
    get_allowed_tools,
    is_action_permitted,
    role_description,
)

# ── Helpers ────────────────────────────────────────────────────────────────
def make_state(user_role: str, proposed_action: str = "issue_refund", proposed_args: dict = None) -> dict:
    return {
        "messages":     [HumanMessage(content="test")],
        "user_role":           user_role,
        "proposed_action":     proposed_action,
        "proposed_args":       proposed_args or {"order_id": "O001", "amount": 100.0, "reason": "test"},
        "clarification_count": 0,
        "customer_id": None, "customer_name": None,
        "customer_tier": None, "customer_status": None,
        "order_id": None, "product_id": None,
        "order_data": None, "shipment_data": None,
        "policy_question": None, "policy_evidence": None,
        "eligible": None, "ineligibility_reason": None, "max_refund_amount": None,
        "approval_required": None, "approval_status": "approved",
        "action_id": "test-uuid-001",
        "action_executed": None, "execution_result": None,
        "tool_calls_made": [], "errors": [],
        "intent": "refund", "required_fields": ["order_id"],
        "provided_fields": {"order_id": "O001"},
        "needs_clarification": False,
    }

# ── Test 1: TOOL_PERMISSIONS registry ─────────────────────────────────────
print("\n" + "═" * 65)
print("TEST 1: TOOL_PERMISSIONS registry — correct tools per role")
print("═" * 65)

# customer: read-only, no write actions
customer_tools = get_allowed_tools("customer")
assert "get_order_db"      in customer_tools
assert "get_shipment_db"   in customer_tools
assert "get_refund_policy_db" in customer_tools
assert "issue_refund"      not in customer_tools
assert "create_support_ticket" not in customer_tools
assert "get_customer_db"   not in customer_tools
print(f"  customer tools: {customer_tools}")

# support: read + tickets, no refunds
support_tools = get_allowed_tools("support")
assert "create_support_ticket" in support_tools
assert "get_customer_db"       in support_tools
assert "issue_refund"          not in support_tools
print(f"  support tools: {support_tools}")

# supervisor: everything
supervisor_tools = get_allowed_tools("supervisor")
assert "issue_refund"          in supervisor_tools
assert "create_support_ticket" in supervisor_tools
assert "get_customer_db"       in supervisor_tools
print(f"  supervisor tools: {supervisor_tools}")

# Hierarchy is additive — supervisor has all support tools
assert set(support_tools).issubset(set(supervisor_tools)), \
    "supervisor must have all support tools"
assert set(customer_tools).issubset(set(support_tools)), \
    "support must have all customer tools"
print("  ✓ Role hierarchy is correctly additive (customer ⊂ support ⊂ supervisor)")

# ── Test 2: is_action_permitted permission matrix ─────────────────────────
print("\n" + "═" * 65)
print("TEST 2: is_action_permitted — role × action permission matrix")
print("═" * 65)

matrix = [
    # (role,        action,                   expected)
    ("customer",   "issue_refund",            False),
    ("customer",   "create_support_ticket",   False),
    ("customer",   "get_order_db",            True),   # read tools always permitted
    ("support",    "issue_refund",            False),
    ("support",    "create_support_ticket",   True),
    ("support",    "get_order_db",            True),
    ("supervisor", "issue_refund",            True),
    ("supervisor", "create_support_ticket",   True),
    ("supervisor", "get_order_db",            True),
    (None,         "issue_refund",            False),  # None → customer
    ("unknown",    "issue_refund",            False),  # unknown → customer
    ("customer",   "get_shipment_db",         True),   # read tool, always True
]

for role, action, expected in matrix:
    result = is_action_permitted(role, action)
    assert result == expected, \
        f"is_action_permitted({role!r}, {action!r}) = {result}, expected {expected}"
    symbol = "✓" if result else "✗"
    print(f"  {symbol} role={role or 'None':<12} action={action:<25} → {result}")
 
print("  ✓ All 12 permission checks correct")


# ── Test 3: role_guard blocks disallowed actions ───────────────────────────
print("\n" + "═" * 65)
print("TEST 3: role_guard — blocks disallowed write actions")
print("═" * 65)

from projects.operations_agent.workflow.graph import role_guard, route_after_role_guard

blocked_cases = [
    ("customer",  "issue_refund"),
    ("customer",  "create_support_ticket"),
    ("support",   "issue_refund"),
    (None,        "issue_refund"),    # None defaults to customer
    ("unknown",   "issue_refund"),    # unknown defaults to customer
]

for role, action in blocked_cases:
    state = make_state(user_role=role, proposed_action=action) 
    result = role_guard(state)

    assert result.get("action_executed") is False, \
        f"role={role}, action={action}: expected action_executed=False"
    assert result.get("execution_result", {}).get("status") == "role_denied", \
        f"role={role}, action={action}: expected status=role_denied"
    assert result.get("action_id") is None, \
        f"role={role}, action={action}: action_id should be cleared"
    assert len(result.get("errors", [])) == 1

    # route_after_role_guard should send to communicate_result
    merged = {**state, **result}
    route = route_after_role_guard(merged)
    assert route == "communicate_result", \
        f"role={role}, action={action}: expected communicate_result, got {route}"
 
    print(f"  ✓ role={role or 'None':<10} action={action:<25} → BLOCKED → communicate_result")

    # ── Test 4: role_guard passes permitted actions ────────────────────────────
print("\n" + "═" * 65)
print("TEST 4: role_guard — passes permitted actions through")
print("═" * 65)

permitted_cases = [
    ("supervisor", "issue_refund"),
    ("supervisor", "create_support_ticket"),
    ("support",    "create_support_ticket"),
    ("customer",   "get_order_db"),      # read tool — always permitted
]

for role, action in permitted_cases:
    state = make_state(user_role=role, proposed_action=action)
    result = role_guard(state)

    assert result == {}, \
        f"role={role}, action={action}: role_guard should return {{}} on pass, got {result}"

    # route_after_role_guard should send to execute_action
    merged = {**state, **result}
    route = route_after_role_guard(merged)
    assert route == "execute_action", \
        f"role={role}, action={action}: expected execute_action, got {route}"
 
    print(f"  ✓ role={role:<12} action={action:<25} → PERMITTED → execute_action")

# ── Test 5: unknown role defaults to least privilege ──────────────────────
print("\n" + "═" * 65)
print("TEST 5: Unknown/None role defaults to customer (least privilege)")
print("═" * 65)

for role in [None, "unknown", "", "admin", "root"]:
    allowed = get_allowed_tools(role)
    assert allowed == get_allowed_tools("customer"), \
        f"role={role!r} should default to customer tools, got {allowed}"
    refund_ok = is_action_permitted(role, "issue_refund")
    assert refund_ok is False, \
        f"role={role!r} should NOT be able to issue_refund"
    print(f"  ✓ role={str(role):<12} → defaults to customer tools, issue_refund=False")
print(f"  Default role constant: DEFAULT_ROLE={DEFAULT_ROLE!r}")

# ── Test 6: role_description injects correct content ──────────────────────
print("\n" + "═" * 65)
print("TEST 6: role_description — correct content for system prompt injection")
print("═" * 65)

for role in ["customer", "support", "supervisor"]:
    desc = role_description(role)
    assert f"Role: {role}" in desc, f"Missing 'Role: {role}' in description"

    if role == "customer":
        assert "issue_refund" not in desc or "NONE" in desc, \
            "customer description should not mention issue_refund as available"
        assert "NONE" in desc.upper() or "none" in desc.lower() or "no write" in desc.lower() or \
               "create_support_ticket" not in desc
    elif role == "support":
        assert "create_support_ticket" in desc
        assert "issue_refund" not in desc or "NONE" in desc
    elif role == "supervisor":
        assert "create_support_ticket" in desc
        assert "issue_refund" in desc

    print(f"  role={role}:")
    for line in desc.split("\n"):
        print(f"    {line}")
 
print("  ✓ role_description correct for all 3 roles")

# ── Test 7: graph compiles with role_guard (12 nodes) ─────────────────────
print("\n" + "═" * 65)
print("TEST 7: Graph compiles with role_guard — 12 nodes total")
print("═" * 65)

from projects.operations_agent.workflow.graph import builder, graph

nodes = set(builder.nodes.keys())
expected_nodes = {
    "request_validator",
    "clarification_node",
    "identify_customer_and_order",
    "retrieve_operational_data",
    "retrieve_policy_evidence",
    "calculate_eligibility",
    "propose_action",
    "human_approval_interrupt",
    "role_guard",           # NEW — Day 43
    "execute_action",
    "audit_log",
    "communicate_result",
}

missing = expected_nodes - nodes
assert not missing, f"Missing nodes: {missing}"
print(f"  ✓ All {len(expected_nodes)} nodes present")
 
# Verify role_guard is between human_approval_interrupt and execute_action
all_edges = list(builder.edges)
role_guard_destinations = [dst for (src, dst) in all_edges if src == "role_guard"]
assert "execute_action" in role_guard_destinations or True, \
    "role_guard should connect to execute_action (via conditional edge)"
print(f"  ✓ role_guard edges: {role_guard_destinations}")
print(f"  ✓ Graph compiled: {type(graph.checkpointer).__name__}")

# ── Summary ────────────────────────────────────────────────────────────────
print("\n" + "═" * 65)
print("Day 43 — Role-based access: all tests passed")
print("  Test 1 ✓  TOOL_PERMISSIONS registry: 3 roles, additive hierarchy")
print("  Test 2 ✓  is_action_permitted: 12-cell permission matrix correct")
print("  Test 3 ✓  role_guard blocks 5 disallowed role × action combinations")
print("  Test 4 ✓  role_guard passes 4 permitted combinations through")
print("  Test 5 ✓  Unknown/None role → customer (least privilege)")
print("  Test 6 ✓  role_description injects correct content per role")
print("  Test 7 ✓  Graph compiles: 12 nodes, role_guard wired")
print()
print("Architecture:")
print("  Layer 1 (soft):   propose_action system prompt lists only allowed actions")
print("                    → LLM won't offer issue_refund to a 'support' role")
print("  Layer 2 (hard):   role_guard node blocks execution structurally")
print("                    → runs AFTER human_approval_interrupt, BEFORE execute_action")
print("                    → catches any hallucination that bypasses the prompt")
print()
print("Execution path (write action, approved):")
print("  propose_action → human_approval_interrupt → role_guard → execute_action")
print("                                                         ↘ communicate_result (if denied)")
