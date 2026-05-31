# Project context for Claude Code

This is the Regulatory Research Assistant — a multi-agent RAG system over FDA guidance documents. Portfolio project; quality bar is "senior AI engineer interviewer should be impressed."

## Read first for design questions

- **`docs/decisions/index.md`** — the ADR index. **Before proposing any architectural alternative** (orchestration framework, model provider, vector store, identity layer, observability, deployment target, new dependency), grep `docs/decisions/` for the topic. Read matching ADRs filtered by `Status: Active`. Read superseded ones only for historical context.
- **`docs/spec.md`** — design with rejected alternatives.
- **`docs/future-work.md`** — deferred items with reopening triggers.
- **`docs/dev-log.md`** — running journal; source of truth for "where are we right now."

For routine work (typos, errors, debugging) just help — no need to pull these in.

## How to use ADRs

- **Active ADRs are current truth.** Don't silently contradict one.
- **If a request would conflict with an Active ADR**, say so, name the ADR, and ask whether to update the ADR (via supersession) or find a different path.
- **If a request matches a rejected alternative in an Active ADR**, the reasoning in that ADR is the default answer; surface it.
- **When proposing a new architectural decision**, draft an ADR using `docs/decisions/0000-template.md` rather than just writing code.
- **Never edit the Decision section of an Active or Superseded ADR.** Write a new ADR that supersedes it (see `docs/decisions/README.md`).

## Stack notes worth knowing

- Python 3.11+, **`uv`** for packages (never pip, never conda). Add deps with `uv add`.
- LangGraph for orchestration. Anthropic SDK direct, not LangChain wrappers (ADR 0001, 0003).
- Postgres + pgvector pulls double duty as application state AND vector store (ADR 0002).
- Custom MCP server with stdio + HTTP transports. The `check_citation` tool is what makes the system distinctive.
- Langfuse self-hosted via docker-compose (regulated-vertical narrative).

## Two rules that catch real foot-guns

1. **All config flows through `src/rra/config.py`** (Pydantic Settings). Never `os.getenv` or `os.environ` anywhere else. Secrets are `SecretStr`; call `.get_secret_value()` only at the boundary.

2. **Before declaring a change "done," run the eval harness.** Unit tests passing is not enough. If `citation_validity` drops, the change is wrong even if tests are green:
   ```bash
   uv run python -m rra.evals.run
   ```

## Working style preferences

- **For non-trivial modules: ask whether to start with structure (signatures + execution order + key decisions) or jump to implementation.** I'm learning; structure-first is often better but not always.
- **Push back when you disagree with my approach**, with the reason. Don't push back for the sake of it. Be honest.
- **When making a unilateral decision in unattended work, log it in the dev-log.** If the decision is architectural, draft an ADR for review.

## Commits

- Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`.
- New dependencies need a one-line justification in the commit body.
- Decision changes need a matching ADR (and a doc update in the same commit if `spec.md` is affected).
