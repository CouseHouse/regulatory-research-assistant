"""Tests for rra.api: FastAPI endpoint behaviour.

Day 4 note: api.py now delegates to run_graph() instead of calling
search_corpus + Anthropic directly. The mocking layer has been updated
to patch rra.api.run_graph; all assertions are unchanged (contract preserved).
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from rra.api import app
from rra.schemas import QueryResponse, RetrievedPassage

# ─── Fixtures ──────────────────────────────────────────────────────────────────

VALID_KEY = "dev-key-change-me"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def sample_passage() -> RetrievedPassage:
    return RetrievedPassage(
        guidance_id="72674",
        guidance_title="Software as a Medical Device (SaMD): Clinical Evaluation",
        chunk_index=3,
        text=(
            "SaMD requires a risk-based approach to software validation aligned with "
            "the intended use and the significance of the information provided by "
            "the SaMD to the healthcare decision."
        ),
        char_start=1024,
        char_end=1280,
        score=0.94,
    )


def _make_graph_state(
    draft: str,
    passages: list[RetrievedPassage],
    verdict: str = "approve",
    cap_hit: bool = False,
) -> dict[str, Any]:
    """Build a minimal final-state dict as returned by run_graph()."""
    return {
        "query": "",
        "product_context": "",
        "session_id": "test-session",
        "trace_id": None,
        "sub_questions": ["test sub-question"],
        "outline": "",
        "passages": passages,
        "draft": draft,
        "verdict": verdict,
        "critic_notes": [],
        "revision_count": 0,
        "cap_hit": cap_hit,
        "token_usage": {},
    }


@pytest.fixture
def mocked_stack(sample_passage: RetrievedPassage) -> Any:
    """Patch run_graph so no real I/O occurs."""
    answer = (
        "SaMD requires a risk-based approach to validation. "
        "[72674:3]<q>a risk-based approach to software validation</q> "
        "The intended use drives the safety classification."
    )
    mock_state = _make_graph_state(answer, [sample_passage])
    with patch("rra.api.run_graph", return_value=mock_state):
        yield


# ─── Auth ──────────────────────────────────────────────────────────────────────

def test_missing_api_key_returns_401(client: TestClient) -> None:
    resp = client.post("/query", json={"query": "test"})
    assert resp.status_code == 401


def test_wrong_api_key_returns_401(client: TestClient) -> None:
    resp = client.post(
        "/query",
        json={"query": "test"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_correct_api_key_is_accepted(
    client: TestClient, mocked_stack: Any
) -> None:
    resp = client.post(
        "/query",
        json={"query": "SaMD validation"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200


# ─── Response schema ───────────────────────────────────────────────────────────

def test_response_parses_as_query_response(
    client: TestClient, mocked_stack: Any
) -> None:
    """200 response validates against the frozen QueryResponse schema."""
    resp = client.post(
        "/query",
        json={"query": "What are the SaMD validation requirements?"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200
    parsed = QueryResponse.model_validate(resp.json())
    assert parsed.answer != ""
    assert isinstance(parsed.citations, list)
    assert isinstance(parsed.passages, list)


def test_response_includes_passages(
    client: TestClient, mocked_stack: Any, sample_passage: RetrievedPassage
) -> None:
    resp = client.post(
        "/query",
        json={"query": "SaMD"},
        headers={"X-API-Key": VALID_KEY},
    )
    data = resp.json()
    assert len(data["passages"]) == 1
    assert data["passages"][0]["guidance_id"] == sample_passage.guidance_id
    assert data["passages"][0]["chunk_index"] == sample_passage.chunk_index


# ─── Citation resolution (ADR 0006) ────────────────────────────────────────────

def test_inline_citation_resolved_to_full_citation(
    client: TestClient, mocked_stack: Any
) -> None:
    """[72674:3] in the answer becomes a Citation with all five fields."""
    resp = client.post(
        "/query",
        json={"query": "SaMD validation"},
        headers={"X-API-Key": VALID_KEY},
    )
    data = resp.json()
    assert len(data["citations"]) >= 1

    cit = data["citations"][0]
    assert cit["guidance_id"] == "72674"
    assert cit["chunk_index"] == 3
    assert cit["char_start"] == 1024
    assert cit["char_end"] == 1280
    # ADR 0013: quoted_text is the analyst's emitted <q> span, not a chunk slice.
    assert cit["quoted_text"] == "a risk-based approach to software validation"


def test_quoted_text_is_analyst_span_not_chunk_slice(client: TestClient) -> None:
    """ADR 0013 (supersedes ADR 0006's substring clause): quoted_text is the
    analyst's OWN emitted span — not a slice of the chunk, and NOT required to be
    a literal substring of it. The resolver surfaces the analyst's words verbatim;
    faithfulness is verified out-of-band by check_citation (see test_mcp_tools.py),
    not asserted by the API resolver.

    This is the updated form of the old test_quoted_text_is_substring_of_chunk:
    the substring invariant is intentionally inverted, not removed (Sign-off B).
    """
    # Stored chunk carries a PDF-embedded newline (the documented dirty-corpus
    # artifact). The analyst quotes the whitespace-normalized span, which is
    # therefore NOT a raw substring of the stored text — yet it is exactly what
    # we surface; the matching engine (not the resolver) tolerates the whitespace.
    passage = RetrievedPassage(
        guidance_id="72674",
        guidance_title="SaMD",
        chunk_index=3,
        text="SaMD requires a risk-based\napproach to software validation.",
        char_start=0,
        char_end=60,
        score=0.9,
    )
    quote = "a risk-based approach to software validation"
    assert quote not in passage.text  # not a literal substring — the newline differs

    answer = f"SaMD validation matters. [72674:3]<q>{quote}</q> Done."
    mock_state = _make_graph_state(answer, [passage])
    with patch("rra.api.run_graph", return_value=mock_state):
        resp = client.post(
            "/query",
            json={"query": "SaMD validation"},
            headers={"X-API-Key": VALID_KEY},
        )

    data = resp.json()
    cit = next(c for c in data["citations"] if c["guidance_id"] == "72674")
    assert cit["quoted_text"] == quote  # analyst's span, surfaced verbatim
    assert cit["quoted_text"] != passage.text[:150]  # NOT the old [:150] slice


def test_citation_without_quote_resolves_to_empty_quoted_text(
    client: TestClient,
) -> None:
    """ADR 0013: a citation with no <q> resolves to quoted_text="" — NEVER a chunk
    slice (no slice fallback; plan §7-#1 / drop-point D-E). The address still
    resolves; only the quote is absent.
    """
    passage = RetrievedPassage(
        guidance_id="doc",
        guidance_title="D",
        chunk_index=0,
        text="Some regulatory requirement text about device labeling.",
        char_start=0,
        char_end=55,
        score=0.9,
    )
    answer = "A claim with no supporting quote. [doc:0]"
    mock_state = _make_graph_state(answer, [passage])
    with patch("rra.api.run_graph", return_value=mock_state):
        resp = client.post(
            "/query", json={"query": "x"}, headers={"X-API-Key": VALID_KEY}
        )

    data = resp.json()
    cit = next(c for c in data["citations"] if c["guidance_id"] == "doc")
    assert cit["quoted_text"] == ""  # honest no-quote signal
    assert cit["quoted_text"] != passage.text[:150]  # the slice would be non-empty


def test_q_markers_stripped_from_answer(
    client: TestClient, mocked_stack: Any
) -> None:
    """The <q>…</q> envelope never leaks into the user-facing answer; the inline
    [guid:idx] marker is kept (plan §6-#4)."""
    resp = client.post(
        "/query", json={"query": "x"}, headers={"X-API-Key": VALID_KEY}
    )
    answer = resp.json()["answer"]
    assert "<q>" not in answer and "</q>" not in answer
    assert "[72674:3]" in answer


def test_unresolvable_citation_skipped(client: TestClient) -> None:
    """Citations referencing passages not in the result set are silently dropped."""
    passage = RetrievedPassage(
        guidance_id="real-doc",
        guidance_title="Real Guidance",
        chunk_index=0,
        text="Real guidance text about regulatory requirements.",
        char_start=0,
        char_end=50,
        score=0.9,
    )
    # Answer cites a passage NOT in the retrieved set
    answer = "Some answer. [real-doc:0] And a phantom citation. [ghost-doc:99]"

    mock_state = _make_graph_state(answer, [passage])
    with patch("rra.api.run_graph", return_value=mock_state):
        resp = client.post(
            "/query",
            json={"query": "test"},
            headers={"X-API-Key": VALID_KEY},
        )

    data = resp.json()
    assert resp.status_code == 200
    citation_keys = [(c["guidance_id"], c["chunk_index"]) for c in data["citations"]]
    assert ("real-doc", 0) in citation_keys
    assert ("ghost-doc", 99) not in citation_keys  # phantom dropped, not 500


def test_duplicate_citations_deduplicated(client: TestClient) -> None:
    """The same [guid:idx] appearing twice produces one Citation object."""
    passage = RetrievedPassage(
        guidance_id="doc-a",
        guidance_title="Doc A",
        chunk_index=1,
        text="Important text about requirements.",
        char_start=0,
        char_end=34,
        score=0.85,
    )
    answer = "Point one [doc-a:1] and point two also [doc-a:1]."

    mock_state = _make_graph_state(answer, [passage])
    with patch("rra.api.run_graph", return_value=mock_state):
        resp = client.post(
            "/query",
            json={"query": "test"},
            headers={"X-API-Key": VALID_KEY},
        )

    data = resp.json()
    doc_a_cits = [c for c in data["citations"] if c["guidance_id"] == "doc-a"]
    assert len(doc_a_cits) == 1


# ─── Edge cases ────────────────────────────────────────────────────────────────

def test_empty_corpus_returns_200_not_500(client: TestClient) -> None:
    """No retrieved passages → 200 with empty answer and lists."""
    mock_state = _make_graph_state("", [])
    with patch("rra.api.run_graph", return_value=mock_state):
        resp = client.post(
            "/query",
            json={"query": "anything"},
            headers={"X-API-Key": VALID_KEY},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["citations"] == []
    assert data["passages"] == []


def test_product_context_accepted(
    client: TestClient, mocked_stack: Any
) -> None:
    """product_context field is accepted without error."""
    resp = client.post(
        "/query",
        json={
            "query": "software validation",
            "product_context": "Class II AI-enabled diagnostic device",
        },
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200


def test_missing_query_field_returns_422(client: TestClient) -> None:
    """Missing required query field → 422 validation error."""
    resp = client.post(
        "/query",
        json={"product_context": "no query"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 422


def test_empty_query_returns_422(client: TestClient) -> None:
    """Empty string for query → 422 (min_length=1 constraint)."""
    resp = client.post(
        "/query",
        json={"query": ""},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 422
