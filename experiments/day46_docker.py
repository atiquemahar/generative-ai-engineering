# experiments/day46_docker.py
# Day 46 — Dockerize
#
# Tests:
#   1. API app object is a FastAPI instance and all routers are registered
#   2. GET /health returns 200 with {status, db, timestamp} schema
#   3. POST /sessions validates request schema (422 on missing message field)
#   4. GET /metrics/dashboard returns all 4 metric categories
#   5. Dockerfile has all required production directives
#
# Tests 1-4 use FastAPI's TestClient — no Azure credentials needed.
# Integration test (full graph run) is @pytest.mark.integration.
#
# Run:
#   pytest experiments/day46_docker.py -v
#   pytest experiments/day46_docker.py -v --integration   # includes session test
#
# Standalone:
#   python experiments/day46_docker.py

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
 
import pytest
 
# ── Use a test SQLite DB so tests never touch the dev database ────────────────
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_day46.db")

# ═══════════════════════════════════════════════════════════════════════════════
# Test 1 — API app is a FastAPI instance with all routers registered
# ═══════════════════════════════════════════════════════════════════════════════

def test_app_is_fastapi_with_all_routers():
    """
    Validates:
      - FastAPI app object is importable
      - All three route prefixes are registered (/health, /sessions, /metrics)
      - OpenAPI schema contains the expected tags
    """
    from fastapi import FastAPI
    from projects.operations_agent.api.main import app

    assert isinstance(app, FastAPI), "app must be a FastAPI instance"

    # Collect all registered routes
    schema = app.openapi()
    paths = set(schema["paths"])

    assert "/health" in paths, "/health route not registered"
    assert "/sessions" in paths, "POST /sessions route not registered"
    assert "/metrics/dashboard" in paths, "/metrics/dashboard route not registered"

    tags = {
        tag
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict)
        for tag in operation.get("tags", [])
    } 
    for expected_tag in ("health", "sessions", "metrics"):
        assert expected_tag in tags, f"Router tag '{expected_tag}' missing from OpenAPI spec"

    print(f"\n  ✓ FastAPI app registered {len(paths)} routes")
    print(f"  ✓ Tags present: {sorted(tags)}")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2 — GET /health returns 200 with correct schema
# ═══════════════════════════════════════════════════════════════════════════════

def test_health_endpoint():
    """
    Validates:
      - Status code 200
      - Response body has {status, db, timestamp}
      - status is "ok" (SQLite reachable with test DB)
      - timestamp is a non-empty string (ISO 8601 UTC)
    """
    from fastapi.testclient import TestClient
    from projects.operations_agent.api.main import app

    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    body = resp.json()
    for field in ("status", "db", "timestamp"):
        assert field in body, f"Field '{field}' missing from /health response"

    assert body["status"] == "ok", f"Expected status='ok', got {body['status']!r}"
    assert body["db"] == "ok", f"Expected db='ok', got {body['db']!r}"
    assert body["timestamp"], "timestamp is empty"
 
    print(f"\n  ✓ /health → 200")
    print(f"  ✓ status: {body['status']!r}")
    print(f"  ✓ db:     {body['db']!r}")
    print(f"  ✓ ts:     {body['timestamp']}")

# ═══════════════════════════════════════════════════════════════════════════════
# Test 3 — POST /sessions validates schema (422 on missing required field)
# ═══════════════════════════════════════════════════════════════════════════════

def test_sessions_schema_validation():
    """
    Validates:
      - Empty body → 422 Unprocessable Entity (not 500)
      - Missing 'message' field → 422
      - Invalid user_role doesn't crash the validator (FastAPI accepts any string)
      - Valid minimal body is accepted (201 or runs to interrupt/complete)
 
    Note: we do NOT call graph.invoke() here — we test schema gating only.
    The 422 path is pure Pydantic validation, no LLM or graph execution.
    """  
    from fastapi.testclient import TestClient
    from projects.operations_agent.api.main import app

    with TestClient(app, raise_server_exceptions=False) as client:

        # ── Empty body → 422 ──────────────────────────────────────────────────
        resp = client.post("/sessions", json={})
        assert resp.status_code == 422, (
            f"Empty body should return 422, got {resp.status_code}"
        )

        # ── Missing message field → 422 ───────────────────────────────────────
        resp = client.post("/sessions", json={"user_role": "customer"})
        assert resp.status_code == 422, (
            f"Missing 'message' should return 422, got {resp.status_code}"
        )

        # ── Extra unknown fields are silently ignored (Pydantic v2 default) ───
        # (No assertion needed — just verifying it doesn't 500)
        resp = client.post(
            "/sessions",
            json={"message": "hi", "user_role": "customer", "unknown_field": "x"},
        )
        # Will be 201 (complete/interrupted) or 500 only if Azure creds missing —
        # we only assert it's NOT 422 (the schema accepted it)
        assert resp.status_code != 422, "Valid body with extra fields should not 422"

    print(f"\n  ✓ Empty body → 422")
    print(f"  ✓ Missing 'message' → 422")
    print(f"  ✓ Valid body schema accepted (status={resp.status_code})")


def test_metrics_dashboard_structure():
    """
    Validates:
      - GET /metrics/dashboard returns 200
      - Response has all 4 top-level metric category keys
      - Latency category has 4 metric keys
      - Token usage category has 4 metric keys
      - Operational category has 4 distribution keys
      - queried_at and since meta-fields are present
 
    Does NOT require Azure — reads from audit_logs (may be empty for new DB).
    """

    from fastapi.testclient import TestClient
    from projects.operations_agent.api.main import app
 
    with TestClient(app) as client:
        resp = client.get("/metrics/dashboard")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
 
    body = resp.json()

    # ── Top-level categories ──────────────────────────────────────────────────
    for category in ("latency", "token_usage", "operational", "queried_at", "since"):
        assert category in body, f"Category '{category}' missing from dashboard response"

    # ── Latency: 4 keys ───────────────────────────────────────────────────────
    lat = body["latency"]
    for key in (
        "avg_retrieval_latency_ms",
        "max_retrieval_latency_ms",
        "avg_propose_action_latency_ms",
        "max_propose_action_latency_ms",
    ):
        assert key in lat, f"Latency metric '{key}' missing"

    # ── Token usage: 4 keys ───────────────────────────────────────────────────
    tok = body["token_usage"]
    for key in (
        "avg_propose_input_tokens",
        "avg_propose_output_tokens",
        "total_propose_input_tokens",
        "total_propose_output_tokens",
    ):
        assert key in tok, f"Token metric '{key}' missing"

    # ── Operational: 4 distribution keys ─────────────────────────────────────
    ops = body["operational"]
    for key in (
        "execution_outcome_distribution",
        "request_volume_by_intent",
        "role_distribution",
        "retrieval_method_distribution",
    ):
        assert key in ops, f"Operational metric '{key}' missing"
 
    print(f"\n  ✓ /metrics/dashboard → 200")
    print(f"  ✓ latency keys:    {list(lat.keys())}")
    print(f"  ✓ token keys:      {list(tok.keys())}")
    print(f"  ✓ operational keys:{list(ops.keys())}")
    print(f"  ✓ queried_at:      {body['queried_at']}")

# ═══════════════════════════════════════════════════════════════════════════════
# Test 5 — Dockerfile has all required production directives
# ═══════════════════════════════════════════════════════════════════════════════

def test_dockerfile_has_required_directives():
    """
    Validates the Dockerfile (static text inspection — no Docker daemon needed):
      - Multi-stage: both FROM ... AS base and FROM base AS production
      - HEALTHCHECK directive is present
      - EXPOSE 8000
      - USER appuser (non-root)
      - CMD references uvicorn with the correct module path
      - PYTHONPATH set so monorepo absolute imports work in container
 
    This test passes on any machine with the repo checked out, regardless
    of whether Docker is installed.
    """
    dockerfile = REPO_ROOT / "Dockerfile"
    assert dockerfile.exists(), f"Dockerfile not found at {dockerfile}"

    content = dockerfile.read_text()

    checks = {
        "multi-stage base stage":     "AS base",
        "multi-stage production stage":"AS production",
        "EXPOSE 8000":                "EXPOSE 8000",
        "non-root USER":              "USER appuser",
        "HEALTHCHECK":                "HEALTHCHECK",
        "curl in HEALTHCHECK":        "curl -f http://localhost:8000/health",
        "uvicorn CMD":                "uvicorn",
        "correct module path":        "projects.operations_agent.api.main:app",
        "PYTHONPATH env var":         "PYTHONPATH",
    }

    failed = []
    for description, expected_text in checks.items():
        if expected_text not in content:
            failed.append(f"  ✗ {description}: {expected_text!r} not found")

    if failed:
        pytest.fail("Dockerfile missing required directives:\n" + "\n".join(failed))

    print(f"\n  ✓ All {len(checks)} Dockerfile directives present")
    for description in checks:
        print(f"    ✓ {description}")

# ═══════════════════════════════════════════════════════════════════════════════
# Integration Test — Full session flow without a write action  [--integration]
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
def test_full_session_inform_only():
    """
    Integration test: runs a complete 'What is the refund policy?' session.
    Requires Azure credentials and a deployed KnowledgeAgent index.
 
    Validates:
      - POST /sessions returns 201 with thread_id
      - status is "complete" (no write action, no approval needed)
      - response is a non-empty string
      - intent is "refund_policy"
      - GET /sessions/{thread_id} returns the same state
    """
    from fastapi.testclient import TestClient
    from projects.operations_agent.api.main import app

    with TestClient(app) as client:
        # ── Start session ──────────────────────────────────────────────────────
        resp = client.post(
            "/sessions",
            json={"message": "What is the refund policy?", "user_role": "customer"},
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"

        body = resp.json()
        thread_id = body["thread_id"]
 
        assert body["status"] == "complete", (
            f"Expected status='complete', got {body['status']!r}"
        )
        assert body["response"], "response is empty"
        assert body["intent"] == "refund_policy", (
            f"Expected intent='refund_policy', got {body['intent']!r}"
        )

        # ── GET same session ───────────────────────────────────────────────────
        get_resp = client.get(f"/sessions/{thread_id}")
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["thread_id"] == thread_id
        assert get_body["status"] == "complete"

        # ── Unknown thread returns 404 ─────────────────────────────────────────
        missing = client.get("/sessions/nonexistent-thread-xyz")
        assert missing.status_code == 404
 
    print(f"\n  ✓ Session created: {thread_id}")
    print(f"  ✓ status: {body['status']!r}")
    print(f"  ✓ intent: {body['intent']!r}")
    print(f"  ✓ response snippet: {(body['response'] or '')[:80]}...")

# ═══════════════════════════════════════════════════════════════════════════════
# Standalone runner
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import traceback

    print("=" * 72)
    print("DAY 46 — Dockerize (standalone)")
    print("=" * 72)

    tests = [
        ("Test 1 — FastAPI app + all routers",          test_app_is_fastapi_with_all_routers),
        ("Test 2 — GET /health → 200 + schema",         test_health_endpoint),
        ("Test 3 — POST /sessions schema validation",   test_sessions_schema_validation),
        ("Test 4 — GET /metrics/dashboard structure",   test_metrics_dashboard_structure),
        ("Test 5 — Dockerfile required directives",     test_dockerfile_has_required_directives),
    ]

    passed = 0
    for name, fn in tests:
        print(f"\n{name}")
        print("-" * 60)
        try:
            fn()
            print("  PASS")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL — {e}")
        except Exception as e:
            print(f"  ERROR — {type(e).__name__}: {e}")
            traceback.print_exc()
 
    print("\n" + "=" * 72)
    print(f"Results: {passed}/{len(tests)} passed")
    print("=" * 72)
 
    # Cleanup test DB
    import gc
    test_db = REPO_ROOT / "test_day46.db"
    if test_db.exists():
        gc.collect()   # nudge SQLAlchemy to release the connection
        try:
            test_db.unlink()
            print("  [cleanup] test_day46.db removed")
        except PermissionError:
            print("  [cleanup] test_day46.db still locked — delete manually if needed")   
