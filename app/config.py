"""All settings come from environment variables, so no secret ever lives in the code.

Copy `.env.example` to `.env`, fill it in, and export it before starting the app.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass
class Settings:
    # Where the SQLite file lives.
    database_url: str = field(default_factory=lambda: _get("GREENLIGHT_DATABASE_URL", "sqlite:///greenlight.db"))
    # Folder where repositories are checked out for each incident.
    workspace_dir: str = field(default_factory=lambda: _get("GREENLIGHT_WORKSPACE", "./workspaces"))

    # GitHub App credentials (see docs/setup-github-app.md).
    github_app_id: str = field(default_factory=lambda: _get("GITHUB_APP_ID"))
    github_private_key_path: str = field(default_factory=lambda: _get("GITHUB_PRIVATE_KEY_PATH"))
    github_webhook_secret: str = field(default_factory=lambda: _get("GITHUB_WEBHOOK_SECRET"))

    # AI model. Any OpenAI-compatible endpoint works: OpenAI, Azure OpenAI,
    # Google Gemini's OpenAI-compatible endpoint, a local Ollama server, etc.
    llm_provider: str = field(default_factory=lambda: _get("LLM_PROVIDER", "openai_compatible"))
    llm_base_url: str = field(default_factory=lambda: _get("LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: _get("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _get("LLM_MODEL", ""))
    # Prices per 1 million tokens, in US dollars, used to estimate cost. Set them from
    # your provider's current price page; leave at 0 if unknown.
    llm_input_price_per_m: float = field(default_factory=lambda: _get_float("LLM_INPUT_PRICE_PER_M", 0.0))
    llm_output_price_per_m: float = field(default_factory=lambda: _get_float("LLM_OUTPUT_PRICE_PER_M", 0.0))

    # Sandbox: "docker" for real isolation, "local" ONLY for development on your own demo repos.
    sandbox_backend: str = field(default_factory=lambda: _get("SANDBOX_BACKEND", "docker"))
    sandbox_image: str = field(default_factory=lambda: _get("SANDBOX_IMAGE", "greenlight-sandbox:latest"))
    sandbox_timeout_seconds: int = field(default_factory=lambda: _get_int("SANDBOX_TIMEOUT_SECONDS", 300))
    sandbox_memory: str = field(default_factory=lambda: _get("SANDBOX_MEMORY", "1g"))
    sandbox_cpus: str = field(default_factory=lambda: _get("SANDBOX_CPUS", "1.0"))

    # Budgets that always stop the agent.
    max_agent_steps: int = field(default_factory=lambda: _get_int("MAX_AGENT_STEPS", 15))
    max_repair_attempts: int = field(default_factory=lambda: _get_int("MAX_REPAIR_ATTEMPTS", 2))
    max_cost_usd_per_incident: float = field(default_factory=lambda: _get_float("MAX_COST_USD_PER_INCIDENT", 0.50))
    max_incident_seconds: int = field(default_factory=lambda: _get_int("MAX_INCIDENT_SECONDS", 600))
    flaky_reruns: int = field(default_factory=lambda: _get_int("FLAKY_RERUNS", 3))

    # Worker loop: how often (seconds) the worker checks the jobs table.
    worker_poll_seconds: float = field(default_factory=lambda: _get_float("WORKER_POLL_SECONDS", 1.0))
    start_worker: bool = field(default_factory=lambda: _get("START_WORKER", "1") == "1")


settings = Settings()


def reload_settings() -> Settings:
    """Re-read environment variables (used by tests)."""
    global settings
    settings = Settings()
    return settings
