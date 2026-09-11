"""Felt's config system.

Every environment-specific value (model endpoints, API keys, device
addresses) flows through here — env vars / .env, never hardcoded in code.
See the project README for why this matters.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class FeltConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FELT_", env_file=".env", extra="ignore")

    # Local model backend (default brain — see decoupling principle in the README)
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"

    # Small, dedicated router model for tool-vs-chat decisions (2026-09-11
    # finding: the chat model itself is unreliable at this under a mixed
    # chat+tool-call prompt — a separate narrow classifier is more robust).
    router_model: str = "qwen2.5:1.5b"

    # ntfy — consuming Mario's existing alert topic (assistant-automation.md),
    # not a new notification system. Unset = the listener simply doesn't run.
    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_user: str | None = None
    ntfy_password: str | None = None

    # Cloud delegation is a tool the model can call, not a router swap — see README
    ninerouter_url: str | None = None
    ninerouter_api_key: str | None = None

    # Home location, for location-dependent skills (weather, etc.) — never
    # hardcoded, unset by default, must be set for this deployment.
    home_latitude: float | None = None
    home_longitude: float | None = None

    # Persistence
    db_path: str = "./data/felt.db"

    # Plugin discovery
    plugins_dir: str = "./plugins"

    # HTTP/WebSocket frontend API
    host: str = "0.0.0.0"
    port: int = 8420


def load_config() -> FeltConfig:
    return FeltConfig()
