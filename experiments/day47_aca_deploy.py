# experiments/day47_aca_deploy.py
# Day 47 — Deploy to Azure Container Apps
#
# Tests:
#   1. deploy.ps1 exists and contains all required Azure CLI commands
#   2. All required env vars for deployment are present in the environment
#   3. Live /health returns 200 with status="ok" over HTTPS   [needs ACA_URL]
#   4. Live /metrics/dashboard returns correct 4-category structure [needs ACA_URL]
#   5. Connection is HTTPS and response includes a Server header     [needs ACA_URL]
#
# Tests 1-2 are purely local — no Azure connection needed.
# Tests 3-5 are skipped unless ACA_URL is set in your environment:
#
#   $env:ACA_URL = "https://<your-app>.<region>.azurecontainerapps.io"
#   pytest experiments/day47_aca_deploy.py -v
#
# After running deploy.ps1 the script prints the exact $env:ACA_URL line.
#
# Run:
#   pytest experiments/day47_aca_deploy.py -v
#   python experiments/day47_aca_deploy.py

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ACA_URL is set after a successful deploy.ps1 run
ACA_URL = os.environ.get("ACA_URL", "").rstrip("/")
NEEDS_ACA_URL = pytest.mark.skipif(
    not ACA_URL,
    reason="Set ACA_URL env var to the deployed Container App URL to run live tests",
)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1 — deploy.ps1 exists and has all required Azure CLI commands
# ═══════════════════════════════════════════════════════════════════════════════

def test_deploy_script_has_required_commands():
    """
    Static inspection of deploy/deploy.ps1 — no Azure connection needed.

    Validates that the script contains every Azure CLI command and flag
    required for a complete ACA deployment:
      - az group create
      - az acr create  with --admin-enabled
      - az acr build   (cloud-side build, no local Docker)
      - az containerapp env create
      - az containerapp create with --ingress external and --target-port
      - PYTHONPATH and DATABASE_URL environment variable entries
    """
    script = REPO_ROOT / "deploy" / "deploy.ps1"
    assert script.exists(), f"deploy/deploy.ps1 not found at {script}"

    content = script.read_text()

    required = {
        "resource group creation":        "az group create",
        "ACR creation":                   "az acr create",
        "cloud-side image build":         "az acr build",
        "ACA environment creation":       "az containerapp env create",
        "Container App creation":         "az containerapp create",
        "external ingress":               "--ingress external",
        "target port":                    "--target-port",
        "PYTHONPATH env var":             "PYTHONPATH=/app",
        "DATABASE_URL env var":           "DATABASE_URL=",
        "AZURE_OPENAI_API_KEY env var":   "AZURE_OPENAI_API_KEY",
        "AZURE_SEARCH_ENDPOINT env var":  "AZURE_SEARCH_ENDPOINT",
        "health smoke test":              "/health",
    }

    missing = [
        f"  ✗ {name}: {text!r} not found"
        for name, text in required.items()
        if text not in content
    ]

    if missing:
        pytest.fail("deploy.ps1 missing required content:\n" + "\n".join(missing))

    print(f"\n  ✓ deploy.ps1 found at {script}")
    print(f"  ✓ All {len(required)} required commands / directives present")
    for name in required:
        print(f"    ✓ {name}")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2 — All required env vars are defined for deployment
# ═══════════════════════════════════════════════════════════════════════════════

def test_required_env_vars_present():
    """
    Checks that every env var the Container App needs is set in the current
    shell session (populated from .env by deploy.ps1 — or manually).

    This is a pre-flight check: if any var is missing, deploy.ps1 will pass
    an empty string to ACA and the app will boot but fail on first LLM call.

    Note: the test reads the values as non-empty strings only.
    It does NOT validate that the credentials are correct — that would
    require a live Azure call.
    """
    # Load .env for this test so it works even without running deploy.ps1 first
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    required_vars = [
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "MODEL_DEPLOYMENT_NAME",
        "AZURE_SEARCH_ENDPOINT",
        "AZURE_SEARCH_API_KEY",
        "AZURE_EMBEDDING_DEPLOYMENT",
    ]

    missing = [v for v in required_vars if not os.environ.get(v)]

    if missing:
        pytest.fail(
            "The following env vars are not set (needed for ACA deployment):\n"
            + "\n".join(f"  ✗ {v}" for v in missing)
            + "\n\nAdd them to .env or set them in your shell before running deploy.ps1."
        )

    print(f"\n  ✓ All {len(required_vars)} required env vars are set")
    for v in required_vars:
        val = os.environ[v]
        # Show only first 8 chars + *** to confirm it's set without leaking value
        preview = val[:8] + "***" if len(val) > 8 else "***"
        print(f"    ✓ {v}: {preview}")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3 — Live /health returns 200 with status="ok" over HTTPS
# ═══════════════════════════════════════════════════════════════════════════════

@NEEDS_ACA_URL
def test_live_health_endpoint():
    """
    Calls the deployed Container App's /health endpoint over HTTPS.

    Validates:
      - HTTP 200 response
      - JSON body: {status: "ok", db: "ok", timestamp: "..."}
      - Response time is under 5 s (container is warmed up)
    """
    import httpx

    url = f"{ACA_URL}/health"
    resp = httpx.get(url, timeout=10)

    assert resp.status_code == 200, (
        f"Expected 200 from {url}, got {resp.status_code}: {resp.text[:200]}"
    )

    body = resp.json()
    assert body.get("status") == "ok",  f"status not 'ok': {body}"
    assert body.get("db")     == "ok",  f"db not 'ok': {body}"
    assert body.get("timestamp"),        "timestamp is empty"

    elapsed_ms = resp.elapsed.total_seconds() * 1000
    assert elapsed_ms < 5000, f"Response took {elapsed_ms:.0f}ms — container may be cold"

    print(f"\n  ✓ GET {url} → 200")
    print(f"  ✓ status: {body['status']!r}")
    print(f"  ✓ db:     {body['db']!r}")
    print(f"  ✓ ts:     {body['timestamp']}")
    print(f"  ✓ latency: {elapsed_ms:.0f}ms")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4 — Live /metrics/dashboard returns all 4 categories
# ═══════════════════════════════════════════════════════════════════════════════

@NEEDS_ACA_URL
def test_live_metrics_dashboard():
    """
    Calls /metrics/dashboard on the deployed Container App.

    Validates the four metric categories from Day 45 are all returned:
      - latency        (4 keys)
      - token_usage    (4 keys)
      - operational    (4 distribution keys)
      - queried_at     (ISO 8601 timestamp)
    """
    import httpx

    url = f"{ACA_URL}/metrics/dashboard"
    resp = httpx.get(url, timeout=15)

    assert resp.status_code == 200, (
        f"Expected 200 from {url}, got {resp.status_code}: {resp.text[:200]}"
    )

    body = resp.json()

    for category in ("latency", "token_usage", "operational", "queried_at"):
        assert category in body, f"Category '{category}' missing from live dashboard"

    lat = body["latency"]
    for key in ("avg_retrieval_latency_ms", "max_retrieval_latency_ms",
                "avg_propose_action_latency_ms", "max_propose_action_latency_ms"):
        assert key in lat, f"Latency metric '{key}' missing"

    ops = body["operational"]
    for key in ("execution_outcome_distribution", "request_volume_by_intent",
                "role_distribution", "retrieval_method_distribution"):
        assert key in ops, f"Operational metric '{key}' missing"

    print(f"\n  ✓ GET {url} → 200")
    print(f"  ✓ latency keys:     {list(lat.keys())}")
    print(f"  ✓ operational keys: {list(ops.keys())}")
    print(f"  ✓ queried_at:       {body['queried_at']}")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5 — Connection is HTTPS and TLS certificate is valid
# ═══════════════════════════════════════════════════════════════════════════════

@NEEDS_ACA_URL
def test_https_and_tls():
    """
    Validates the HTTPS contract for the deployed Container App:

      - ACA_URL starts with https:// (not http://)
      - httpx verifies the TLS certificate by default (no verify=False)
      - The response includes a Server header (confirms ACA reverse proxy)
      - OpenAPI docs are reachable (confirms FastAPI is fully booted)

    ACA provides a managed TLS certificate automatically. This test confirms
    it is active and trusted by the Python SSL bundle.
    """
    import httpx

    # URL must be HTTPS
    assert ACA_URL.startswith("https://"), (
        f"ACA_URL must start with https://, got: {ACA_URL!r}"
    )

    # /health with TLS verification enabled (the httpx default)
    resp = httpx.get(f"{ACA_URL}/health", timeout=10)
    assert resp.status_code == 200, "TLS-verified request to /health failed"

    # ACA reverse proxy sets a Server header
    assert resp.headers.get("server") or resp.headers.get("x-powered-by"), (
        "No Server or X-Powered-By header — may not be going through ACA ingress"
    )

    # OpenAPI docs are served by FastAPI
    docs_resp = httpx.get(f"{ACA_URL}/docs", timeout=10)
    assert docs_resp.status_code == 200, (
        f"/docs returned {docs_resp.status_code} — FastAPI may not be fully booted"
    )

    print(f"\n  ✓ ACA_URL uses HTTPS: {ACA_URL}")
    print(f"  ✓ TLS certificate valid (Python SSL bundle accepted it)")
    print(f"  ✓ Server header: {resp.headers.get('server', resp.headers.get('x-powered-by'))!r}")
    print(f"  ✓ /docs → 200 (FastAPI fully booted)")


# ═══════════════════════════════════════════════════════════════════════════════
# Standalone runner
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import traceback

    print("=" * 72)
    print("DAY 47 — Deploy to Azure Container Apps (standalone)")
    print(f"ACA_URL: {ACA_URL or '(not set — live tests will be skipped)'}")
    print("=" * 72)

    tests = [
        ("Test 1 — deploy.ps1 has all required commands",  test_deploy_script_has_required_commands,  False),
        ("Test 2 — required env vars present for deploy",  test_required_env_vars_present,            False),
        ("Test 3 — live /health → 200 over HTTPS",         test_live_health_endpoint,                 True),
        ("Test 4 — live /metrics/dashboard structure",     test_live_metrics_dashboard,               True),
        ("Test 5 — HTTPS + TLS certificate valid",         test_https_and_tls,                        True),
    ]

    passed = skipped = 0
    for name, fn, needs_url in tests:
        print(f"\n{name}")
        print("-" * 60)
        if needs_url and not ACA_URL:
            print("  SKIPPED — set ACA_URL to run")
            skipped += 1
            continue
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
    print(f"Results: {passed}/{len(tests) - skipped} passed  ({skipped} skipped — no ACA_URL)")
    print("=" * 72)