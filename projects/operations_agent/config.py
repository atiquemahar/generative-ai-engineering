# projects/operations_agent/config.py
# Day 48 — Azure Key Vault + Managed Identity
#
# Single source of truth for all required configuration.
#
# Why this file exists:
#   Before Day 48, `os.environ["AZURE_OPENAI_API_KEY"]` was scattered across
#   graph.py, database/engine.py, and knowledge_agent. A misconfigured
#   Container App would boot successfully and fail only on the first LLM call.
#
#   Settings.from_env() is called during FastAPI's lifespan (main.py startup).
#   A missing env var raises ValueError immediately at boot — the container
#   fails its HEALTHCHECK, ACA marks it unhealthy, and traffic is never routed
#   to it. "Fail fast at startup" is better than "fail silently at 3am."
#
# How secrets flow in production (Day 48):
#   Key Vault secret → ACA Key Vault reference → ACA env var → os.environ
#   The application sees normal env vars. No SDK change needed.
#
# How secrets flow in local dev:
#   .env file → python-dotenv → os.environ → Settings.from_env()
#
# Usage in main.py lifespan:
#   from projects.operations_agent.config import get_settings
#   settings = get_settings()   # raises ValueError if any required var is missing
#
# Usage in tests:
#   from projects.operations_agent.config import Settings, REQUIRED_VARS
#   settings = Settings.from_env()

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

# ── Canonical list of required env vars ───────────────────────────────────────
# Any var in this list must be set before the app accepts traffic.
# Optional vars are read with .get() and have documented defaults.
REQUIRED_VARS: list[str] = [
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "MODEL_DEPLOYMENT_NAME",
    "AZURE_SEARCH_ENDPOINT",
    "AZURE_SEARCH_API_KEY",
    "AZURE_EMBEDDING_DEPLOYMENT",
]

# Optional vars — the app degrades gracefully when these are absent
OPTIONAL_VARS: list[str] = [
    "AZURE_PROJECT_ENDPOINT",
    "APPLICATIONINSIGHTS_CONNECTION_STRING",
    "DATABASE_URL",
]


@dataclass(frozen=True)
class Settings:
    """
    Immutable settings object.  All fields are populated from environment
    variables — never from hardcoded values in this file.

    frozen=True prevents accidental mutation after startup.
    """
    # ── Required ──────────────────────────────────────────────────────────────
    azure_openai_api_key:      str
    azure_openai_endpoint:     str
    model_deployment_name:     str
    azure_search_endpoint:     str
    azure_search_api_key:      str
    azure_embedding_deployment: str

    # ── Optional ──────────────────────────────────────────────────────────────
    azure_project_endpoint:              str = ""
    applicationinsights_connection_string: str = ""
    database_url:                        str = "sqlite:///./operations_agent.db"

    @classmethod
    def from_env(cls) -> "Settings":
        """
        Build Settings from the current environment.

        Raises ValueError listing ALL missing required vars at once
        (not just the first one) so the operator can fix everything
        in a single deploy cycle.
        """
        missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
        if missing:
            raise ValueError(
                "Container App is missing required environment variables. "
                "Check Key Vault secret references in ACA configuration.\n"
                "Missing: " + ", ".join(missing)
            )

        return cls(
            azure_openai_api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_openai_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            model_deployment_name=os.environ["MODEL_DEPLOYMENT_NAME"],
            azure_search_endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
            azure_search_api_key=os.environ["AZURE_SEARCH_API_KEY"],
            azure_embedding_deployment=os.environ["AZURE_EMBEDDING_DEPLOYMENT"],
            azure_project_endpoint=os.environ.get("AZURE_PROJECT_ENDPOINT", ""),
            applicationinsights_connection_string=os.environ.get(
                "APPLICATIONINSIGHTS_CONNECTION_STRING", ""
            ),
            database_url=os.environ.get(
                "DATABASE_URL", "sqlite:///./operations_agent.db"
            ),
        )

    def redacted_repr(self) -> dict:
        """
        Safe representation for logging — shows which vars are set
        without leaking any values.

        Example output:
            {"AZURE_OPENAI_API_KEY": "SET", "MODEL_DEPLOYMENT_NAME": "SET", ...}
        """
        return {
            "AZURE_OPENAI_API_KEY":       "SET" if self.azure_openai_api_key       else "MISSING",
            "AZURE_OPENAI_ENDPOINT":      "SET" if self.azure_openai_endpoint      else "MISSING",
            "MODEL_DEPLOYMENT_NAME":      "SET" if self.model_deployment_name      else "MISSING",
            "AZURE_SEARCH_ENDPOINT":      "SET" if self.azure_search_endpoint      else "MISSING",
            "AZURE_SEARCH_API_KEY":       "SET" if self.azure_search_api_key       else "MISSING",
            "AZURE_EMBEDDING_DEPLOYMENT": "SET" if self.azure_embedding_deployment else "MISSING",
            "AZURE_PROJECT_ENDPOINT":     "SET" if self.azure_project_endpoint     else "EMPTY (optional)",
            "APPLICATIONINSIGHTS_CONNECTION_STRING":
                                          "SET" if self.applicationinsights_connection_string else "EMPTY (optional)",
            "DATABASE_URL":               self.database_url,
        }


# ── Module-level lazy singleton ───────────────────────────────────────────────
# Populated once on first call to get_settings().
# FastAPI lifespan calls this at startup; subsequent callers get the cached object.
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """
    Return the cached Settings singleton.
    Calls Settings.from_env() on first call — raises ValueError if any
    required env var is missing.
    """
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_settings() -> None:
    """
    Clear the cached Settings object.
    Used in tests to allow re-loading after os.environ changes.
    """
    global _settings
    _settings = None