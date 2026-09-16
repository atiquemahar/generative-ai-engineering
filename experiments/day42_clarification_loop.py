# experiments/day42_clarification_loop.py
# Clarification loop verification
#
# Tests:
#   Test 1 — clarification_node loops back to request_validator (not END)
#   Test 2 — Full-history intent recovery: "Refund please" → "order O003"
#             validator correctly resolves intent=refund, order_id=O003
#   Test 3 — clarification_count increments and resets correctly
#   Test 4 — Give-up after MAX_CLARIFICATION_ATTEMPTS: routes to END
#   Test 5 — WorkflowState has clarification_count field
#
# Run:
#   python experiments/day42_clarification_loop.py

import sys
from pathlib import Path
 
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from langchain_core.messages import HumanMessage, AIMessage

from projects.operations_agent.graph.request_validator import (
    request_validator,
    clarification_node,
    route_after_validation,
    MAX_CLARIFICATION_ATTEMPTS,
    _all_user_text,
)

# ── Helpers ────────────────────────────────────────────────────────────────
def make_state(messages: list, clarification_count: int  = 0) -> dict:
    """Minimal state dict for testing validator and clarification_node."""
    return {
        "messages":           messages,
        "clarification_count": clarification_count,
        "customer_id":        None, "customer_name": None,
        "customer_tier":      None, "customer_status": None,
        "order_id":           None, "product_id": None,
        "order_data":         None, "shipment_data": None,
        "policy_question":    None, "policy_evidence": None,
        "eligible":           None, "ineligibility_reason": None,
        "max_refund_amount":  None,
        "proposed_action":    None, "proposed_args": None,
        "approval_required":  None, "approval_status": None,
        "action_id":          None, "action_executed": None,
        "execution_result":   None, "user_role": None,
        "tool_calls_made": [], "errors": [],
        "intent": None, "required_fields": [], "provided_fields": {},
        "needs_clarification": None,
    }

def apply(state: dict, updates: dict) -> dict:
    """Merge node output into state (simulates LangGraph state merging)."""
    merged = {**state, **updates}
    if "messages" in updates:
        # Append new messages to existing history (add_messages reducer behaviour)
        existing = state.get("messages", [])
        merged["messages"] = existing + updates["messages"]
    return merged


# ── Test 1: clarification_node loops back (edge check) ────────────────────
print("\n" + "═" * 65)
print("TEST 1: clarification_node → request_validator (not END)")
print("═" * 65)

from projects.operations_agent.workflow.graph import builder

edges = dict(builder.edges)
# In LangGraph, builder.edges is a set of (src, dst) tuples
all_edges = list(builder.edges)
clar_destinations = [dst for (src, dst) in all_edges if src == "clarification_node"]

assert "request_validator" in clar_destinations,  (
    f"clarification_node must route to request_validator, got: {clar_destinations}"
)

assert "__end__" not in clar_destinations and "END" not in str(clar_destinations), (
    f"clarification_node must NOT route directly to END, got: {clar_destinations}"
)
print(f"  ✓ clarification_node → {clar_destinations}")


# ── Test 2: Full-history intent recovery ──────────────────────────────────
print("\n" + "═" * 65)
print("TEST 2: Full-history intent recovery across 2 turns")
print("═" * 65)

# --- Turn 1: customer says "Refund please" (missing order_id) ---
state_t1 = make_state([HumanMessage(content="Refund please")])

v1 = request_validator(state_t1)
state_t1 = apply(state_t1, v1)

assert state_t1["intent"] == "refund", f"Turn 1: expected intent=refund, got {state_t1['intent']}"
assert state_t1["needs_clarification"] is True, "Turn 1: should need clarification (no order_id)"
assert state_t1["clarification_count"] == 0, \
    f"Turn 1: count should still be 0 before clarification_node, got {state_t1['clarification_count']}"
 
route_t1 = route_after_validation(state_t1)
assert route_t1 == "clarification_node", f"Turn 1: expected clarification_node, got {route_t1}"
print(f"  Turn 1: intent=refund, needs_clarification=True → route={route_t1} ✓")

# clarification_node runs, increments count
c1 = clarification_node(state_t1)
state_t1 = apply(state_t1, c1)

assert state_t1["clarification_count"] == 1, \
    f"After clarification_node: count should be 1, got {state_t1['clarification_count']}"
print(f"  clarification_count incremented to {state_t1['clarification_count']} ✓")
print(f"  Clarification message: '{state_t1['messages'][-1].content}'")

# --- Turn 2: customer replies "Oh sorry, order O003" ---
# Simulate: customer sends new message to the SAME thread
state_t2 = apply(state_t1, {
    "messages": [HumanMessage(content="Oh sorry, order O003")]
})

# Verify _all_user_text sees both messages
all_text = _all_user_text(state_t2)
assert "Refund" in all_text, "Full history should contain 'Refund'"
assert "O003" in all_text, "Full history should contain 'O003'"
print(f"  Full history text: '{all_text}'")

v2 = request_validator(state_t2)
state_t2 = apply(state_t2, v2)
 
assert state_t2["intent"] == "refund", \
    f"Turn 2: expected intent=refund (from history), got {state_t2['intent']}"
assert state_t2["provided_fields"].get("order_id") == "O003", \
    f"Turn 2: expected order_id=O003, got {state_t2['provided_fields']}"
assert state_t2["needs_clarification"] is False, \
    f"Turn 2: should NOT need clarification now (order_id provided)"
assert state_t2["clarification_count"] == 0, \
    f"Turn 2: count should reset to 0 on success, got {state_t2['clarification_count']}"
 
route_t2 = route_after_validation(state_t2)
assert route_t2 == "agent_node", f"Turn 2: expected agent_node, got {route_t2}"
 
print(f"  Turn 2: intent=refund, order_id=O003, needs_clarification=False")
print(f"  clarification_count reset to {state_t2['clarification_count']} ✓")
print(f"  Route: {route_t2} ✓")
print("  ✓ Full-history intent recovery works correctly")

# ── Test 3: clarification_count increments and resets ─────────────────────
print("\n" + "═" * 65)
print("TEST 3: clarification_count increments per ask, resets on success")
print("═" * 65)
 
# Start fresh
s = make_state([HumanMessage(content="hello")])   # unknown intent
 
v = request_validator(s)
s = apply(s, v)
assert s["clarification_count"] == 0
 
c = clarification_node(s)
s = apply(s, c)
assert s["clarification_count"] == 1, f"Expected 1, got {s['clarification_count']}"
print(f"  After 1st clarification: count={s['clarification_count']} ✓")

# Customer still unclear
s = apply(s, {"messages": [HumanMessage(content="I'm not sure")]})
v = request_validator(s)
s = apply(s, v)
c = clarification_node(s)
s = apply(s, c)
assert s["clarification_count"] == 2, f"Expected 2, got {s['clarification_count']}"
print(f"  After 2nd clarification: count={s['clarification_count']} ✓")
 
# Customer finally provides a valid request
s = apply(s, {"messages": [HumanMessage(content="I want a refund for order O001")]})
v = request_validator(s)
s = apply(s, v)
assert s["needs_clarification"] is False
assert s["clarification_count"] == 0, \
    f"Count should reset to 0 on valid request, got {s['clarification_count']}"
print(f"  On valid request: count reset to {s['clarification_count']} ✓")

# ── Test 4: Give-up after MAX_CLARIFICATION_ATTEMPTS ─────────────────────
print("\n" + "═" * 65)
print(f"TEST 4: Give-up after MAX_CLARIFICATION_ATTEMPTS={MAX_CLARIFICATION_ATTEMPTS}")
print("═" * 65)
 
s = make_state([HumanMessage(content="help")])  # unknown
 
# Run through MAX attempts
for attempt in range(1, MAX_CLARIFICATION_ATTEMPTS + 1):
    v = request_validator(s)
    s = apply(s, v)
    route = route_after_validation(s)
 
    if route == "__end__":
        print(f"  Attempt {attempt}: route=__end__ (give up) ✓")
        break
 
    assert route == "clarification_node", f"Attempt {attempt}: expected clarification_node, got {route}"
    c = clarification_node(s)
    s = apply(s, c)
    print(f"  Attempt {attempt}: route=clarification_node, count now={s['clarification_count']}")

    # Simulate customer sending another unhelpful reply
    s = apply(s, {"messages": [HumanMessage(content="I dunno")]})
else:
    # One final validation after the last customer message
    v = request_validator(s)
    s = apply(s, v)
    route = route_after_validation(s)
    assert route == "__end__", \
        f"After {MAX_CLARIFICATION_ATTEMPTS} attempts: expected __end__, got {route}"
    print(f"  Final attempt: route=__end__ (give up) ✓")
 
print(f"  ✓ Graph routes to END after {MAX_CLARIFICATION_ATTEMPTS} failed clarifications")

# ── Test 5: WorkflowState has clarification_count ─────────────────────────
print("\n" + "═" * 65)
print("TEST 5: WorkflowState.clarification_count field present and typed int")
print("═" * 65)
 
import typing
from projects.operations_agent.workflow.state import WorkflowState
 
hints = typing.get_type_hints(WorkflowState, include_extras=True)
assert "clarification_count" in hints, \
    "clarification_count missing from WorkflowState"
 
# Should be int (not Optional[int]) — it always has a value (defaults to 0)
field_type = hints["clarification_count"]
assert field_type is int or str(field_type) == "<class 'int'>", \
    f"clarification_count should be int, got {field_type}"
 
print(f"  ✓ clarification_count: {field_type}")

# ── Summary ────────────────────────────────────────────────────────────────
print("\n" + "═" * 65)
print("Clarification loop — all tests passed")
print(f"  Test 1 ✓  clarification_node → request_validator (not END)")
print(f"  Test 2 ✓  Full-history intent recovery: 2-turn refund conversation")
print(f"  Test 3 ✓  clarification_count increments and resets correctly")
print(f"  Test 4 ✓  Give-up after {MAX_CLARIFICATION_ATTEMPTS} failed attempts → END")
print(f"  Test 5 ✓  WorkflowState.clarification_count field typed as int")
print()
print("Architecture summary:")
print("  OLD: clarification_node → END  (session closed, no memory)")
print("  NEW: clarification_node → request_validator  (session lives)")
print("       request_validator uses _all_user_text() in loop mode")
print("       so turn-2 'order O003' resolves turn-1 'refund' intent")
print(f"       give-up guard: MAX_CLARIFICATION_ATTEMPTS={MAX_CLARIFICATION_ATTEMPTS}")
