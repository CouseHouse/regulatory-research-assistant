# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# App image for the Regulatory Research Assistant FastAPI gateway.
#
# Multi-stage, uv-driven (project standard — never pip/conda). The final image
# carries only a self-contained venv and runs as a non-root user.
#
# SECRETS: this image NEVER contains .env or any API key. .dockerignore excludes
# .env and nothing here copies it — secrets are injected at runtime by the ECS
# task definition from Secrets Manager (infra/terraform/ecs.tf + secrets.tf).
# ─────────────────────────────────────────────────────────────────────────────

# ── Builder: resolve + install deps and the rra package into /app/.venv ──────
FROM python:3.11-slim-bookworm AS builder

# Pinned uv (matches the version that resolved uv.lock) for reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Layer 1 — dependencies only. Cached unless pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Layer 2 — the project itself. README.md is required by the hatchling build
# (pyproject `readme = "README.md"`); src holds the package. --no-editable
# installs a real wheel so the runtime image needs no source tree.
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ── Runtime: just the venv, non-root ─────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8000

# ALB health-checks GET /health (unauthenticated). /query requires X-API-Key.
CMD ["uvicorn", "rra.api:app", "--host", "0.0.0.0", "--port", "8000"]

# ── Bootstrap: one-off corpus ingest into a fresh (private) RDS ───────────────
# Separate image from the serving one (ADR 0017). `rra.ingest` needs
# data/corpus/manifest.json AND a layout where config.PROJECT_ROOT resolves to
# the repo root. So this stage ships the source tree + the manifest and installs
# the project EDITABLE (no --no-editable): config.py then lives at /app/src/rra/,
# so PROJECT_ROOT = parents[2] = /app and DATA_DIR = /app/data/corpus exists.
# The cached corpus PDFs ARE baked in (ADR 0018, superseding 0017): FDA/Akamai blocks
# the Fargate datacenter IP, so re-downloading at runtime 4xx-fails. ingest hits the
# cache (dest.exists()) and skips the fetch.
#
# Run as a one-off ECS run-task in the private subnets on the ecs_tasks SG
# (infra/terraform/bootstrap.tf). It creates the vector extension + schema and
# embeds the corpus; RDS never becomes publicly accessible. Idempotent: re-run
# safe (CREATE ... IF NOT EXISTS + upsert ON CONFLICT).
FROM python:3.11-slim-bookworm AS bootstrap

COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

# Dependencies first (cache layer), then the project + corpus data.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY README.md ./
COPY src ./src
# Brings data/corpus/manifest.json (the 45 MB of PDFs are excluded by .dockerignore).
COPY data ./data
# editable install (no --no-editable) → config.PROJECT_ROOT resolves to /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# DATA_DIR.mkdir() writes downloaded PDFs under /app/data/corpus at runtime.
RUN chown -R app:app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER app

# Default = FULL corpus (no --limit → ingest.main ingests every manifest entry).
# Override via the ECS task `command` (e.g. ["--limit","50"] for a demo subset, or
# ["--truncate"]). Image default is full so an empty task `command=[]` resolves to
# the full corpus under either ECS empty-override interpretation (var default is []).
ENTRYPOINT ["python", "-m", "rra.ingest"]
CMD []
