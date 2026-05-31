"""Tests for rra.api: FastAPI endpoint behaviour.

Unit tests mock search_corpus and the Anthropic client so no network or DB
calls are made.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from typing import Any
from unittest.mock import MagicMock, patch

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


def _make_anthropic_mock(answer_text: str) -> Any:
    """Fake Anthropic client whose messages.create() returns *answer_text*."""
    from anthropic.types import TextBlock

    text_block = TextBlock(type="text", text=answer_text)
    mock_msg = MagicMock()
    mock_msg.content = [text_block]
    mock_msg.usage = MagicMock(input_tokens=600, output_tokens=120)

    mock_anthropic = MagicMock()
    mock_anthropic.messages.create.return_value = mock_msg
    return mock_anthropic


@pytest.fixture
def mocked_stack(sample_passage: RetrievedPassage) -> Any:
    """Patch search_corpus and Anthropic so no real I/O occurs."""
    answer = (
        "SaMD requires a risk-based approach to validation. [72674:3] "
        "The intended use drives the safety classification."
    )
    with (
        patch("rra.api.search_corpus", return_value=[sample_passage]),
        patch("rra.api.Anthropic", return_value=_make_anthropic_mock(answer)),
    ):
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
    assert len(cit["quoted_text"]) > 0


def test_quoted_text_is_substring_of_chunk(
    client: TestClient, mocked_stack: Any, sample_passage: RetrievedPassage
) -> None:
    """ADR 0006: quoted_text must be a verified substring of the stored chunk text."""
    resp = client.post(
        "/query",
        json={"query": "SaMD validation"},
        headers={"X-API-Key": VALID_KEY},
    )
    data = resp.json()
    for cit in data["citations"]:
        if cit["guidance_id"] == sample_passage.guidance_id and cit["chunk_index"] == sample_passage.chunk_index:
            assert cit["quoted_text"] in sample_passage.text, (
                "quoted_text is not a substring of the stored chunk text — ADR 0006 violation"
            )


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

    with (
        patch("rra.api.search_corpus", return_value=[passage]),
        patch(
            "rra.api.Anthropic",
            return_value=_make_anthropic_mock(answer),
        ),
    ):
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

    with (
        patch("rra.api.search_corpus", return_value=[passage]),
        patch("rra.api.Anthropic", return_value=_make_anthropic_mock(answer)),
    ):
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
    with patch("rra.api.search_corpus", return_value=[]):
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
