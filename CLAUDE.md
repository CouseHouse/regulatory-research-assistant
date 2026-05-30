# Project context for Claude Code

This is the Regulatory Research Assistant — a multi-agent RAG system over FDA guidance documents. It's a portfolio project; the quality bar is "a senior AI engineer interviewer should be impressed."

## Read first for design questions

- **`docs/spec.md`** — every architectural decision with rejected alternatives.
- **`docs/future-work.md`** — items deferred from v1 with triggers that would reopen them.
- **`docs/dev-log.md`** — running journal of decisions and surprises; the source of truth for "where are we right now."

For routine work (fix this typo, explain this error, debug this exception) just help — no need to pull these in.

## Stack notes worth knowing

- Python 3.11+, **`uv`** for packages (never pip, never conda). Add deps with `uv add`.
- LangGraph for orchestration. Anthropic SDK direct, not LangChain wrappers.
- Postgres + pgvector pulls double duty as application state store AND vector store. There is no second data store; that's intentional.
- Custom MCP server with stdio + HTTP transports. The `check_citation` tool is what makes the system distinctive.
- Langfuse self-hosted via docker-compose (regulated-vertical narrative).

The rest is in `pyproject.toml` and the spec.

## Two rules that catch real foot-guns

1. **All config flows through `src/rra/config.py`** (Pydantic Settings). Never `os.getenv` or `os.environ` anywhere else. Secrets are `SecretStr`; call `.get_secret_value()` only at the boundary.

2. **Before declaring a change "done," run the eval harness.** Unit tests passing is not enough. If `citation_validity` drops, the change is wrong even if tests are green:
   ```bash
   uv run python -m rra.evals.run
   ```

## Working style preferences

- **For non-trivial modules: ask whether to start with structure (signatures + execution order + key decisions) or jump to implementation.** I'm learning; structure-first is often better but not always.
- **If a request contradicts `spec.md`, say so and ask before proceeding.** Either we update the spec or find a spec-consistent path. Don't silently deviate; don't refuse to help — flag it and let me decide.
- **Push back when you disagree with my approach**, with the reason. Don't push back for the sake of it. Just be honest.

## Commits

- Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`.
- New dependencies need a one-line justification in the commit body.
- Decision changes need a matching `docs/` update in the same commit.
