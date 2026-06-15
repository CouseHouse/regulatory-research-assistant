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

RRA_PROFILE selects the execution environment.  The ``local`` profile is the
default; adapter phases (Phase 2+) add per-profile adapter selection.  Config
precedence is strict: explicit env vars beat .env beat profile defaults beat
field defaults.  PROFILE_DEFAULTS only fills fields that are absent from both
env and .env — it can NEVER override a value the operator supplied.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = three levels up from this file (src/rra/config.py → root)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Per-profile default registry
# ---------------------------------------------------------------------------
# Rules:
#  1. Defaults here are applied ONLY if the field was not supplied via env or
#     .env.  Explicit env always wins (see model_validator below).
#  2. NEVER put secret values in this registry.  Secrets must come from the
#     operator (env var, .env, or a secrets manager).
#  3. The ``local`` entry MUST reproduce today's field-level defaults exactly
#     so behaviour is byte-identical under RRA_PROFILE=local and when unset.
#  4. aws / azure / gcp entries are intentionally sparse — adapter phases
#     (Phase 2+) will populate them as each adapter is implemented.
# ---------------------------------------------------------------------------

PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "local": {
        # All local defaults mirror the field-level defaults; see below.
        # They are written out explicitly here so a future reader can see the
        # full local profile at a glance rather than hunting through field defs.
        "planner_model": "claude-sonnet-4-6",
        "analyst_model": "claude-sonnet-4-6",
        "critic_model": "claude-sonnet-4-6",
        "researcher_model": "claude-haiku-4-5",
        "key_fact_judge_model": "claude-haiku-4-5",
        "position_judge_model": "claude-sonnet-4-6",
        "embedding_model": "voyage-3",
        "embedding_dim": 1024,
        "rerank_model": "rerank-2",
        "retrieve_top_k": 25,
        "rerank_top_k": 5,
        "max_critic_revisions": 2,
        "citation_match_threshold": 0.85,
        "chunk_size_tokens": 512,
        "chunk_overlap_tokens": 50,
        "download_rate_per_second": 5.0,
        "download_burst": 10,
        "max_tokens_per_query": 200_000,
        "max_tool_calls_per_query": 20,
        "langfuse_host": "http://localhost:3000",
        "postgres_host": "localhost",
        "postgres_port": 5432,
        "postgres_user": "rra",
        "postgres_db": "rra",
        # Guardrails: SECURE BY DEFAULT — the local profile ships with the
        # real HuggingFace injection detector ON.  Set GUARDRAILS_DETECTOR=allowall
        # in tests (conftest.py does this) to prevent torch from loading.
        "guardrails_detector": "local-hf",
        "guardrail_model": "protectai/deberta-v3-base-prompt-injection-v2",
        "guardrail_model_revision": "e6535ca4ce3ba852083e75ec585d7c8aeb4be4c5",
        "guardrail_threshold": 0.2,
    },
    # aws / azure / gcp: sparse until adapter phases fill them in.
    # No secret values here — ever.
    "aws": {},
    "azure": {},
    "gcp": {},
}


class Settings(BaseSettings):
    """All runtime configuration. Field names map to UPPER_SNAKE_CASE env vars
    automatically (e.g. `anthropic_api_key` ← `ANTHROPIC_API_KEY`)."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # tolerate extra env vars (CI, system, etc.)
    )

    # ─── Profile selection ──────────────────────────────────────────────────
    # Selects the adapter set for this deployment environment.
    # Adapter phases (Phase 2+) wire adapters behind this field; this phase
    # only registers the field and applies per-profile config defaults.
    rra_profile: Literal["local", "aws", "azure", "gcp"] = "local"

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
    # Required — no default. Sourced from .env locally and Secrets Manager in
    # cloud (ecs.tf / bootstrap.tf). A hardcoded fallback would silently let the
    # app run on a known dev credential; fail fast instead. Ignored when
    # database_url is set (it carries its own password).
    postgres_password: SecretStr
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
    # Required — no default. From .env locally, Secrets Manager in cloud. A
    # baked-in default key would be accepted by /query if the env var were
    # missing; fail fast instead.
    rra_api_key: SecretStr

    # ─── Model selection ────────────────────────────────────────────────────
    # Role-to-model mapping rationale lives in docs/spec.md §4.2.
    # Updated Day 4: planner/analyst/critic → claude-sonnet-4-6.
    planner_model: str = "claude-sonnet-4-6"
    analyst_model: str = "claude-sonnet-4-6"
    critic_model: str = "claude-sonnet-4-6"
    researcher_model: str = "claude-haiku-4-5"
    # Key-fact coverage judge: cheap Haiku LLM-as-judge, checks whether the
    # answer states each expected fact. Runs on every eval. (warn-only scorer)
    key_fact_judge_model: str = "claude-haiku-4-5"
    # Position-quality judge: Sonnet LLM-as-judge WITH retrieved passages in
    # context — the anti-reward-hacking scorer. Hedged-but-grounded must beat
    # confident-but-unsupported. (warn-only scorer)
    position_judge_model: str = "claude-sonnet-4-6"

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

    # ─── Guardrails / injection detection ──────────────────────────────────
    # Which guardrails adapter to use.
    # "local-hf" → HFInjectionGuardrails (protectai DeBERTa model, CPU).
    # "allowall" → AllowAllGuardrails (phase-2 wiring; test isolation; cloud stubs).
    # SECURE BY DEFAULT: the local profile ships with "local-hf" so detection is
    # ON in every dev/local run unless explicitly overridden.
    guardrails_detector: Literal["allowall", "local-hf"] = "local-hf"

    # HuggingFace model ID for injection detection.  Apache 2.0, ungated, ~184M
    # DeBERTa params. Change only if replacing the detector with a newer model.
    guardrail_model: str = "protectai/deberta-v3-base-prompt-injection-v2"

    # Exact Hub commit the detector weights are pinned to (RT-8 supply chain).
    # The Python lockfile pins packages, not model weights — a bare repo id would
    # resolve to whatever `main` points at, so a repo re-push could swap weights
    # silently.  Recorded in the security report header so a swap is visible.
    guardrail_model_revision: str = "e6535ca4ce3ba852083e75ec585d7c8aeb4be4c5"

    # Classifier score threshold: INJECTION label score ≥ this → blocked.
    # 0.2 is the secure-by-default operating point ("block unless confidently
    # safe"), not the classifier's argmax (0.5): at the retrieved_content
    # boundary the cost of a block is one dropped passage, so uncertainty
    # resolves toward blocking. On the red-team corpus the margin is wide —
    # benign controls score ≤ 0.011; the one hard look-alike (rt-c03) is
    # misclassified at ANY threshold and is the accepted FP. See ADR 0023 and
    # RT-redteam.md for the by-layer gate this feeds.
    guardrail_threshold: float = Field(default=0.2, ge=0.0, le=1.0)

    # ─── Test / eval gates ──────────────────────────────────────────────────
    # critic_force_verdict: skip the LLM call and emit this verdict instead.
    # Enables deterministic loop testing without live queries. None = production.
    critic_force_verdict: Literal["approve", "revise", "escalate"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _apply_profile_defaults(cls, data: Any) -> Any:
        """Inject per-profile defaults for fields not explicitly provided.

        Precedence (highest → lowest):
          1. Explicit env var (already in *data* by the time this runs)
          2. .env file value (also already in *data*)
          3. Profile default (applied here, only when the key is absent)
          4. Field-level default (pydantic applies these after validation)

        This validator runs in "before" mode so *data* is a raw dict of
        whatever pydantic-settings collected from env + .env sources.  We
        never overwrite a key that is already present.
        """
        if not isinstance(data, dict):
            return data

        profile = str(data.get("rra_profile", "local")).lower()
        defaults = PROFILE_DEFAULTS.get(profile, {})

        for key, value in defaults.items():
            if key not in data:
                data[key] = value

        return data


# Module-level singleton. Importing `settings` from anywhere gives the same
# instance, so the .env file is read exactly once per process.
settings = Settings()
