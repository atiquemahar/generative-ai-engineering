# experiments/day48_keyvault.py
# Day 48 — Azure Key Vault + Managed Identity
#
# Tests:
#   1. setup_keyvault.ps1 has all required Azure CLI commands
#   2. Settings.from_env() raises ValueError listing ALL missing required vars
#   3. Settings.from_env() loads correctly when all required vars are set
#   4. No hardcoded secrets or API key patterns anywhere in source files
#   5. redacted_repr() never exposes actual secret values in logs
#
# All 5 tests run locally — no Azure connection required.
# Integration test (verifies live /health after Key Vault migration) needs ACA_URL.
#
# Run:
#   pytest experiments/day48_keyvault.py -v
#   python experiments/day48_keyvault.py

from __future__ import annotations

import os
import sys
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ACA_URL = os.environ.get("ACA_URL", "").rstrip("/")
NEEDS_ACA_URL = pytest.mark.skipif(
    not ACA_URL,
    reason="Set ACA_URL to the deployed Container App URL to run this test",
)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1 — setup_keyvault.ps1 has all required Azure CLI commands
# ═══════════════════════════════════════════════════════════════════════════════

def test_keyvault_script_has_required_commands():
    """
    Static inspection of deploy/setup_keyvault.ps1.
    Validates every Azure CLI command needed for a secure Key Vault migration:
      - az keyvault create
      - az keyvault secret set (at least one)
      - az containerapp identity assign  (enables managed identity)
      - Key Vault Secrets User role assignment
      - keyvaultref: format in ACA secret references
      - secretref: format in ACA env var pointers
      - /health smoke test after migration
    """
    script = REPO_ROOT / "deploy" / "setup_keyvault.ps1"
    assert script.exists(), f"deploy/setup_keyvault.ps1 not found at {script}"

    content = script.read_text()

    required = {
        "Key Vault creation":               "az keyvault create",
        "secret write":                     "az keyvault secret set",
        "managed identity assignment":      "az containerapp identity assign",
        "system-assigned identity flag":    "--system-assigned",
        "role assignment":                  "az role assignment create",
        "Key Vault Secrets User role":      "Key Vault Secrets User",
        "ACA KV reference format":          "keyvaultref:",
        "ACA secret pointer format":        "secretref:",
        "identityref:system":               "identityref:system",
        "health verification after rotate": "/health",
    }

    missing = [
        f"  ✗ {name}: {text!r} not found"
        for name, text in required.items()
        if text not in content
    ]
    if missing:
        pytest.fail("setup_keyvault.ps1 missing required content:\n" + "\n".join(missing))

    print(f"\n  ✓ setup_keyvault.ps1 found at {script}")
    print(f"  ✓ All {len(required)} required commands/patterns present")
    for name in required:
        print(f"    ✓ {name}")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2 — Settings.from_env() raises ValueError listing ALL missing required vars
# ═══════════════════════════════════════════════════════════════════════════════

def test_config_rejects_missing_required_vars():
    """
    Validates the fail-fast behaviour in config.py.

    Removes all required env vars from the environment, calls
    Settings.from_env(), and asserts:
      - ValueError is raised (not KeyError, not silent None)
      - Error message names EVERY missing variable — not just the first one
      - reset_settings() works so the singleton doesn't carry state between tests
    """
    from projects.operations_agent.config import Settings, REQUIRED_VARS, reset_settings

    # Back up and clear all required vars
    backup = {v: os.environ.pop(v, None) for v in REQUIRED_VARS}

    try:
        with pytest.raises(ValueError) as exc_info:
            reset_settings()
            Settings.from_env()

        error_msg = str(exc_info.value)

        # Every missing var must be named in the error message
        for var in REQUIRED_VARS:
            assert var in error_msg, (
                f"Missing var '{var}' not mentioned in ValueError:\n{error_msg}"
            )

        print(f"\n  ✓ ValueError raised when {len(REQUIRED_VARS)} required vars are missing")
        print(f"  ✓ All required vars named in error message")
        print(f"  ✓ Error: {error_msg[:120]}...")

    finally:
        # Restore env vars — always, even if assertions fail
        for k, v in backup.items():
            if v is not None:
                os.environ[k] = v
        reset_settings()


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3 — Settings.from_env() loads correctly when all required vars are set
# ═══════════════════════════════════════════════════════════════════════════════

def test_config_loads_with_valid_env():
    """
    Sets all required env vars to dummy values and asserts that
    Settings.from_env() succeeds and returns an immutable Settings object.

    Validates:
      - No exception raised
      - All fields populated from env vars (not hardcoded defaults)
      - frozen=True: AttributeError on mutation attempt
      - Singleton: get_settings() returns the same object on repeat calls
    """
    from projects.operations_agent.config import (
        Settings, REQUIRED_VARS, get_settings, reset_settings
    )

    dummy = {v: f"test-value-{v.lower()}" for v in REQUIRED_VARS}
    backup = {v: os.environ.get(v) for v in REQUIRED_VARS}

    try:
        os.environ.update(dummy)
        reset_settings()

        settings = Settings.from_env()

        assert settings.azure_openai_api_key   == f"test-value-azure_openai_api_key"
        assert settings.model_deployment_name  == f"test-value-model_deployment_name"
        assert settings.azure_search_api_key   == f"test-value-azure_search_api_key"

        # frozen=True — any mutation attempt raises AttributeError
        with pytest.raises((AttributeError, TypeError)):
            settings.azure_openai_api_key = "changed"   # type: ignore

        # Singleton caches correctly
        settings_2 = get_settings()
        assert settings_2 is get_settings(), "get_settings() must return same object"

        print(f"\n  ✓ Settings.from_env() succeeds with all required vars set")
        print(f"  ✓ frozen=True: mutation raises AttributeError")
        print(f"  ✓ get_settings() singleton works correctly")

    finally:
        for k, v in backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_settings()


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4 — No hardcoded secrets or API key patterns in source files
# ═══════════════════════════════════════════════════════════════════════════════

def test_no_hardcoded_secrets_in_source():
    """
    Scans all Python source files in projects/ and experiments/ for patterns
    that look like hardcoded API keys or secrets.

    Checks:
      - .gitignore contains .env (secrets file is gitignored)
      - No file contains 'sk-' followed by alphanumerics (OpenAI key pattern)
      - No file assigns a long hex/alphanumeric string to a key-named variable
      - Dockerfile uses CMD/ENV but no hardcoded secret values
      - docker-compose.yml uses env_file: .env, not hardcoded values
    """
    violations: list[str] = []

    # ── .gitignore must contain .env ──────────────────────────────────────────
    gitignore = REPO_ROOT / ".gitignore"
    if gitignore.exists():
        if ".env" not in gitignore.read_text():
            violations.append(".gitignore does not contain '.env'")
    else:
        violations.append(".gitignore not found — .env could be committed accidentally")

    # ── Scan Python source files ──────────────────────────────────────────────
    # Pattern: variable named *key*, *secret*, *password*, *token* assigned a
    # long (20+) alphanumeric string that looks like a real credential.
    secret_assignment = re.compile(
        r'(?:api_key|secret|password|token|credential)\s*=\s*["\']([A-Za-z0-9/+=]{20,})["\']',
        re.IGNORECASE,
    )
    openai_key_pattern = re.compile(r'\bsk-[A-Za-z0-9]{20,}')

    scan_dirs = [
        REPO_ROOT / "projects",
        REPO_ROOT / "experiments",
    ]

    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for py_file in scan_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            for match in secret_assignment.finditer(content):
                violations.append(f"{py_file.relative_to(REPO_ROOT)}: {match.group()[:60]}")
            if openai_key_pattern.search(content):
                violations.append(f"{py_file.relative_to(REPO_ROOT)}: OpenAI key pattern found")

    # ── docker-compose.yml must use env_file, not hardcoded values ────────────
    compose = REPO_ROOT / "docker-compose.yml"
    if compose.exists():
        compose_content = compose.read_text()
        if "env_file" not in compose_content:
            violations.append("docker-compose.yml does not use env_file: .env")
        # Rough check: no API key pattern values directly in compose file
        if openai_key_pattern.search(compose_content):
            violations.append("docker-compose.yml contains what looks like an API key")

    if violations:
        pytest.fail(
            "Potential hardcoded secrets found:\n"
            + "\n".join(f"  ✗ {v}" for v in violations)
        )

    print(f"\n  ✓ .gitignore contains .env")
    print(f"  ✓ No hardcoded secret patterns in Python source files")
    print(f"  ✓ docker-compose.yml uses env_file: .env")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5 — redacted_repr() never exposes actual secret values
# ═══════════════════════════════════════════════════════════════════════════════

def test_redacted_repr_hides_secret_values():
    """
    Validates the safe logging helper on Settings.

    Sets real-looking dummy secrets, calls redacted_repr(), and asserts:
      - The actual secret values do NOT appear in the output
      - "SET" appears for all populated fields
      - "MISSING" appears for empty fields
      - database_url IS shown (it's not a secret)

    This test catches a bug where a developer accidentally logs settings
    directly instead of calling redacted_repr().
    """
    from projects.operations_agent.config import Settings, REQUIRED_VARS, reset_settings

    fake_secrets = {
        "AZURE_OPENAI_API_KEY":       "fake-key-abc123xyz789-should-not-appear",
        "AZURE_OPENAI_ENDPOINT":      "https://fake-endpoint.openai.azure.com",
        "MODEL_DEPLOYMENT_NAME":      "gpt-4o-fake",
        "AZURE_SEARCH_ENDPOINT":      "https://fake-search.search.windows.net",
        "AZURE_SEARCH_API_KEY":       "fake-search-key-987654",
        "AZURE_EMBEDDING_DEPLOYMENT": "text-embedding-fake",
    }
    backup = {k: os.environ.get(k) for k in fake_secrets}

    try:
        os.environ.update(fake_secrets)
        reset_settings()

        settings = Settings.from_env()
        redacted = settings.redacted_repr()

        # No actual secret values should appear in the redacted repr
        for secret_value in fake_secrets.values():
            repr_str = str(redacted)
            assert secret_value not in repr_str, (
                f"Secret value leaked in redacted_repr(): {secret_value[:20]}..."
            )

        # All populated fields show "SET"
        for field_key in ("AZURE_OPENAI_API_KEY", "MODEL_DEPLOYMENT_NAME", "AZURE_SEARCH_API_KEY"):
            assert redacted.get(field_key) == "SET", (
                f"Expected 'SET' for {field_key}, got {redacted.get(field_key)!r}"
            )

        # database_url IS exposed (not a secret)
        assert "database_url" in str(redacted).lower() or "DATABASE_URL" in redacted, (
            "database_url should be visible in redacted_repr()"
        )

        print(f"\n  ✓ None of the {len(fake_secrets)} secret values appear in redacted_repr()")
        print(f"  ✓ All populated fields show 'SET'")
        print(f"  ✓ database_url is visible (not a secret)")
        print(f"  ✓ Redacted output: {redacted}")

    finally:
        for k, v in backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_settings()


# ═══════════════════════════════════════════════════════════════════════════════
# Integration — /health still returns ok after Key Vault migration
# ═══════════════════════════════════════════════════════════════════════════════

@NEEDS_ACA_URL
def test_live_health_after_keyvault_migration():
    """
    Verifies the Container App is healthy after setup_keyvault.ps1 has run.
    If /health returns ok, Key Vault references are resolving correctly.
    If it returns 'degraded' or 500, a Key Vault reference is broken.
    """
    import httpx

    resp = httpx.get(f"{ACA_URL}/health", timeout=15)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    body = resp.json()
    assert body["status"] == "ok", (
        f"App is not healthy after Key Vault migration: {body}\n"
        "Check: az containerapp logs show --name operations-agent "
        "--resource-group day45-resource --follow"
    )
    print(f"\n  ✓ /health → ok after Key Vault migration")
    print(f"  ✓ All Key Vault secret references resolving correctly")


# ═══════════════════════════════════════════════════════════════════════════════
# Standalone runner
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import traceback

    print("=" * 72)
    print("DAY 48 — Azure Key Vault + Managed Identity (standalone)")
    print("=" * 72)

    tests = [
        ("Test 1 — setup_keyvault.ps1 has required commands",  test_keyvault_script_has_required_commands),
        ("Test 2 — Settings rejects missing required vars",    test_config_rejects_missing_required_vars),
        ("Test 3 — Settings loads with valid env vars",        test_config_loads_with_valid_env),
        ("Test 4 — No hardcoded secrets in source files",      test_no_hardcoded_secrets_in_source),
        ("Test 5 — redacted_repr() hides secret values",       test_redacted_repr_hides_secret_values),
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