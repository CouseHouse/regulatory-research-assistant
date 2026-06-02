"""Typed configuration loaded from environment + .env file.

Every module that needs config imports `settings` from here. Do NOT read
os.environ directly anywhere else in the codebase — if it's not in this
file, it's not configurable, by design.

Usage:
    from rra.config import settings

    client = Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
    print(settings.planner_model)

The settings object is a module-level singleton. Loading happens once at
import time, which means:
  - Bad config fails fast at startup, not deep inside a request
  - Tests can override via monkeypatch.setenv() BEFORE importing the
    consumer, or by constructing a fresh Settings() in a fixture

Note on coexistence with Docker Compose:
  The .env file also holds LANGFUSE_ENCRYPTION_KEY, LANGFUSE_NEXTAUTH_SECRET,
  and LANGFUSE_SALT, which are consumed by docker-compose.yml — NOT by this
  application. They're declared as fields below (with sensible defaults so
  Pydantic doesn't complain when they're absent in test environments) but
  the application never reads them.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = three levels up from this file (src/rra/config.py → root)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """All runtime configuration. Field names map to UPPER_SNAKE_CASE env vars
    automatically (e.g. `anthropic_api_key` ← `ANTHROPIC_API_KEY`)."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # tolerate extra env vars (CI, system, etc.)
    )

    # ─── LLM provider credentials ───────────────────────────────────────────
    # SecretStr prevents accidental logging — repr() shows '**********'
    anthropic_api_key: SecretStr
    voyage_api_key: SecretStr

    # ─── Postgres (app DB) ──────────────────────────────────────────────────
    # Provide the full URL OR the component pieces. URL wins if both are set.
    database_url: str | None = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "rra"
    postgres_password: SecretStr = SecretStr("rra_dev_password")
    postgres_db: str = "rra"

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def pg_dsn(self) -> str:
        """The DSN every Postgres-using module should use."""
        if self.database_url:
            return self.database_url
        return (
            f"postgresql://{self.postgres_user}:"
            f"{self.postgres_password.get_secret_value()}@"
            f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ─── Langfuse — application-side (consumed by the SDK) ──────────────────
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def langfuse_enabled(self) -> bool:
        """Convenience flag — Langfuse calls are no-ops when keys are absent.
        Useful so the eval runner can skip trace publishing in CI."""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    # ─── Langfuse — compose-side (consumed by docker-compose.yml only) ──────
    # Declared so they show up in the typed config surface, but the
    # application code never reads them. Defaults are None because they're
    # not required for the app to run — only for `docker compose up`.
    langfuse_encryption_key: SecretStr | None = None
    langfuse_nextauth_secret: SecretStr | None = None
    langfuse_salt: SecretStr | None = None

    # ─── API auth (v1 — single key; OAuth 2.0 in production design) ─────────
    rra_api_key: SecretStr = SecretStr("dev-key-change-me")

    # ─── Model selection ────────────────────────────────────────────────────
    # Role-to-model mapping rationale lives in docs/spec.md §4.2.
    # Updated Day 4: planner/analyst/critic → claude-sonnet-4-6.
    planner_model: str = "claude-sonnet-4-6"
    analyst_model: str = "claude-sonnet-4-6"
    critic_model: str = "claude-sonnet-4-6"
    researcher_model: str = "claude-haiku-4-5"
    judge_model: str = "claude-haiku-4-5"

    # ─── Retrieval tuning ───────────────────────────────────────────────────
    embedding_model: str = "voyage-3"
    embedding_dim: int = 1024  # MUST match the model — see docs/spec.md §4.5
    rerank_model: str = "rerank-2"
    retrieve_top_k: int = Field(default=25, ge=1, le=200)
    rerank_top_k: int = Field(default=5, ge=1, le=50)
    max_critic_revisions: int = Field(default=2, ge=0, le=5)
    citation_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # ─── Chunking ───────────────────────────────────────────────────────────
    # Picked in docs/spec.md §4.4. Change here, then re-ingest the corpus.
    chunk_size_tokens: int = Field(default=512, ge=64, le=4096)
    chunk_overlap_tokens: int = Field(default=50, ge=0, le=512)

    # ─── Download rate limiting ──────────────────────────────────────────────
    # Governs the ingest pipeline's HTTP download rate.  Override via
    # DOWNLOAD_RATE_PER_SECOND and DOWNLOAD_BURST env vars.
    download_rate_per_second: float = 5.0
    download_burst: int = 10

    # ─── Cost guardrails ────────────────────────────────────────────────────
    # Hard caps that the agent layer should refuse to exceed. Belt-and-
    # suspenders against runaway loops; the LangGraph max_critic_revisions
    # is the primary control.
    max_tokens_per_query: int = Field(default=200_000, ge=1000)
    max_tool_calls_per_query: int = Field(default=20, ge=1, le=100)

    # ─── Test / eval gates ──────────────────────────────────────────────────
    # critic_force_verdict: skip the LLM call and emit this verdict instead.
    # Enables deterministic loop testing without live queries. None = production.
    critic_force_verdict: Literal["approve", "revise", "escalate"] | None = None


# Module-level singleton. Importing `settings` from anywhere gives the same
# instance, so the .env file is read exactly once per process.
settings = Settings()
