# experiments/day45_dashboard_test.py
# Day 45 — Populate audit_logs with 5 scenarios then print dashboard metrics
#
# Run:
#   python experiments/day45_dashboard_test.py

import sys
import os
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv()

from langchain_core.messages import HumanMessage
from projects.operations_agent.workflow.graph import graph
from projects.operations_agent.database.seed import seed_data, create_tables

create_tables()
seed_data()

scenarios = [
    # (thread_id, message, role)
    ("s1", "I want a refund for order O003 for customer C001.", "supervisor"),
    ("s2", "What is the refund policy?",                        "customer"),
    ("s3", "Track my shipment for order O001 for customer C001.", "support"),
    ("s4", "I want a refund for order O001 for customer C002.", "supervisor"),
    ("s5", "Check order status for order O002 for customer C001.", "support"),
]

print("\n" + "═" * 65)
print("Running 5 scenarios to populate audit_logs")
print("═" * 65)

for thread_id, message, role in scenarios:
    print(f"\n{thread_id}: {message[:55]}...")
    config = {"configurable": {"thread_id": thread_id}}
    try:
        result = graph.invoke(
            {"messages": [HumanMessage(content=message)], "user_role": role},
            config,
        )
        state   = graph.get_state(config)
        tracing = state.values.get("tracing_data") or {}
        intent  = state.values.get("intent")
        eligible = state.values.get("eligible")
        outcome  = state.values.get("execution_result")
        latency  = tracing.get("propose_action_latency_ms", 0)
        in_tok   = tracing.get("propose_input_tokens", 0)
        out_tok  = tracing.get("propose_output_tokens", 0)

        print(f"  intent:   {intent}")
        print(f"  eligible: {eligible}")
        print(f"  outcome:  {outcome}")
        print(f"  tracing:  propose={latency:.0f}ms  in={in_tok}tok  out={out_tok}tok")

    except Exception as e:
        # s1 and s4 will pause at human_approval_interrupt — expected
        print(f"  PAUSED at interrupt or error: {type(e).__name__}: {e}")

print("\n" + "═" * 65)
print("Dashboard metrics snapshot")
print("═" * 65)

from projects.operations_agent.monitoring.dashboard_metrics import DashboardMetrics

metrics = DashboardMetrics().get_all_metrics()
print(json.dumps(metrics, indent=2, default=str))