# Day 5 — MCP server with `check_citation`

## Goal

Move retrieval and citation verification from agent code into an MCP server. This is the project's distinctive piece — `check_citation` in particular is the differentiator.

## Deliverables

- `src/rra/mcp_server/server.py`: MCP server setup, stdio + HTTP transports
- `src/rra/mcp_server/tools.py`:
  - `search_corpus(query, k, filters)` — wraps day-3 retrieval
  - `fetch_guidance(guidance_id, section?)` — full document retrieval by ID
  - `check_citation(claim, guidance_id, chunk_index, quoted_text?)` — the distinctive tool
  - `list_recent_guidances(since_date)` — building block for future currency check
- Critic agent (`src/rra/agents/critic.py`) updated to call `check_citation` via MCP, not directly
- Researcher agent updated to use `search_corpus` via MCP
- `tests/test_mcp_tools.py`: each tool tested independently

## Design constraints (from spec §4.7)

- MCP server runs in-process (stdio) for v1 — separately-scaling deferred to future-work §8
- Tools return structured Pydantic models, not free dicts
- `check_citation` returns: `{verified: bool, source_text: str, similarity_score: float}`
- All retrieval logic stays a pure function; MCP is a thin wrapper

## Decisions to make

1. Transport for dev: stdio (default) — for production: streaming HTTP. Both should work.
2. `check_citation` matching: **normalized substring + fuzzy fallback is required, not optional.** Exact matching is known to fail on this corpus (stored chunks retain embedded PDF newlines; 74% contain mid-sentence boilerplate headers — a model's normalized quote is not a verbatim substring). Decision: collapse whitespace on both sides, then substring check; fall back to `difflib.SequenceMatcher` ratio above a configurable threshold in `config.py`.
3. Error contract: structured errors (`ToolError` Pydantic model) vs. exceptions vs. result-wrapping?
4. Tool descriptions: invest in these — they're what the agent sees. Each tool needs 2-3 sentence description + example.

## The distinctive piece — `check_citation`

This tool is the one that signals "this engineer thought about RAG reliability." Implementation outline:

> **CORRECTION (Day 5 orientation):** The pseudocode below predates ADR 0006 and must not be built from directly.
>
> **Real signature:** `check_citation(claim, guidance_id, chunk_index, quoted_text?)` — `chunk_index` is the stable address (ADR 0006: integer key, model copies it verbatim); `quoted_text` is supplementary, verified within the resolved chunk.
>
> **Real matching:** whitespace-normalized substring check (collapse `\s+` → single space on both the stored chunk text and the supplied `quoted_text`, then test containment); fall back to `difflib.SequenceMatcher` ratio above a configurable threshold when normalization alone isn't sufficient. Exact `span not in full_text` **fails on this corpus**: chunk text retains embedded PDF newlines and 74% of chunks contain mid-sentence "Contains Nonbinding Recommendations" headers — a model's normalized quote is never a verbatim substring.
>
> See ADR 0006 and docs/day5-design.md for the rationale.

~~superseded pseudocode — signature and matching logic are incorrect; see correction above~~

```python
def check_citation(claim: str, guidance_id: str, span: str) -> CitationCheck:
    """
    Verify a citation: does the cited span actually appear in the named
    guidance, and does it support the claim?

    Returns the source text so the critic agent can score support
    independently of the verifier's judgment.
    """
    full_text = fetch_guidance_text(guidance_id)  # from Postgres
    if span not in full_text:
        return CitationCheck(verified=False, reason="span not in source")
    return CitationCheck(
        verified=True,
        source_text=extract_context(full_text, span, window=200),
        similarity_score=...  # optional; embedding similarity span<>claim
    )
```

The critic uses the returned `source_text` to decide whether the claim is supported, NOT trust the tool's verdict alone. This is the reliability pattern.

## Verification

Connect Claude Desktop to your local MCP server and exercise each tool by hand. This is a real day-5 milestone — proves the tools are usable by any MCP client, not just your agent.

Add a section to your dev log titled "MCP tools tested via Claude Desktop" with a screenshot or transcript.

## Stop conditions

- All four tools callable from Claude Desktop, return valid responses
- Agent code uses MCP, not direct function calls (verify by `grep` for direct retrieval calls in agent code)
- Langfuse trace shows MCP tool calls as nested spans under the critic agent
- Tests pass, mypy clean

## Don't do yet

- The eval harness wiring (day 6)
- Splitting MCP server to its own service (future-work §8)
- Per-tool rate limiting

## Definition of done

Dev-log entry shows: a Claude Desktop screenshot, the four tool descriptions you wrote, one example trace with a critic `check_citation` call, total token impact of moving to MCP (probably small but document).
