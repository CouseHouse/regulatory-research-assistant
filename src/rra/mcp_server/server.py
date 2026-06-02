"""MCP server — thin wrapper exposing tools.py functions to external clients.

Per ADR 0011: tool logic lives in tools.py. This file registers those functions
as MCP handlers via FastMCP. Agents call tools.py directly in-process; this
server exists for external clients (Claude Desktop, protocol-layer tests).

Run standalone:
    uv run python -m rra.mcp_server.server
or via FastMCP's built-in entrypoint via pyproject.toml script registration.
"""
from __future__ import annotations

import structlog
from mcp.server.fastmcp import FastMCP

from rra.mcp_server.tools import (
    CheckCitationInput,
    CitationCheckResult,
    FetchGuidanceInput,
    FetchGuidanceResult,
    GuidanceRecord,
    ListRecentGuidancesInput,
    ListRecentGuidancesResult,
    SearchCorpusResult,
    SearchFilters,
    ToolError,
    check_citation,
    fetch_guidance,
    list_recent_guidances,
    search_corpus,
)

log = structlog.get_logger(__name__)

mcp = FastMCP(
    "Regulatory Research Assistant",
    instructions=(
        "Tools for searching and verifying FDA regulatory guidance documents. "
        "Use search_corpus to retrieve relevant passages, check_citation to verify "
        "that a cited chunk exists and that a quote faithfully appears in it, "
        "fetch_guidance to read a full document, and list_recent_guidances to "
        "discover recently ingested documents."
    ),
)


@mcp.tool(
    description=(
        "Retrieve the most relevant passages from the FDA guidance document corpus for a "
        "natural-language query. Returns passages ranked by semantic similarity and Voyage "
        "rerank score; each passage carries a guidance_id and chunk_index that form a stable "
        "citation address. Use this tool to gather source evidence before drafting or verifying "
        "a regulatory claim."
    )
)
def search_corpus_tool(
    query: str,
    k: int = 5,
    guidance_ids: list[str] | None = None,
) -> SearchCorpusResult:
    """MCP handler: search_corpus."""
    filters = SearchFilters(guidance_ids=guidance_ids) if guidance_ids else None
    try:
        return search_corpus(query=query, k=k, filters=filters)
    except ToolError:
        raise
    except Exception as exc:
        log.error("server.search_corpus.error", error=str(exc))
        raise ToolError(
            code="UNKNOWN",
            message=str(exc),
            tool="search_corpus",
            retryable=True,
        ) from exc


@mcp.tool(
    description=(
        "Retrieve the full text of an FDA guidance document by its guidance_id, assembled "
        "from stored chunks in their original order. Text is returned as stored — per-chunk "
        "PDF artifacts (embedded newlines, boilerplate headers) are present and will be "
        "addressed in the Day 7 ingest fix. Use this tool when you need complete document "
        "context beyond the top retrieved passages."
    )
)
def fetch_guidance_tool(guidance_id: str) -> FetchGuidanceResult:
    """MCP handler: fetch_guidance."""
    return fetch_guidance(guidance_id=guidance_id)


@mcp.tool(
    description=(
        "Verify that a citation [guidance_id:chunk_index] addresses a real corpus chunk, "
        "and when quoted_text is supplied, that the quote faithfully appears in that chunk "
        "using whitespace-normalized substring matching with a configurable fuzzy fallback. "
        "Returns the stored chunk text unconditionally so the caller can independently assess "
        "whether the passage supports the claim. This tool checks factual presence only — "
        "whether the passage supports the claim is the critic's judgment, not this tool's."
    )
)
def check_citation_tool(
    claim: str,
    guidance_id: str,
    chunk_index: int,
    quoted_text: str | None = None,
) -> CitationCheckResult:
    """MCP handler: check_citation."""
    return check_citation(
        claim=claim,
        guidance_id=guidance_id,
        chunk_index=chunk_index,
        quoted_text=quoted_text,
    )


@mcp.tool(
    description=(
        "List FDA guidance documents whose chunks were first ingested since a given date "
        "(ISO 8601: YYYY-MM-DD), returning guidance_id and title for each. Note: ingest date "
        "is used as a proxy for document currency; FDA publication date may differ and is not "
        "yet captured in corpus metadata. Use as a building block for currency checks before "
        "fetching specific documents."
    )
)
def list_recent_guidances_tool(since_date: str) -> ListRecentGuidancesResult:
    """MCP handler: list_recent_guidances."""
    return list_recent_guidances(since_date=since_date)


if __name__ == "__main__":
    mcp.run()
