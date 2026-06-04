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
