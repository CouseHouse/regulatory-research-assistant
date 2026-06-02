"""Three independent scorers, run on every response.

Design notes:

1. Citation validity is DETERMINISTIC, not LLM-graded. Key-existence check
   against corpus.chunks via check_citation (ADR 0010 key-existence mode).
   Day 6 baseline: verifies guidance_id:chunk_index resolves to a real row —
   does NOT check quote faithfulness (activated Day 7, per ADR 0010).
   This is the only HARD gate (CI blocks merges on it).

2. Key fact coverage uses an LLM-as-judge (Haiku). Cheap, fast, runs on
   every change. Warn-only.

3. Position quality uses Sonnet WITH the retrieved passages in the judge's
   context. This is the reward-hacking fix — without source context the
   judge agrees with anything confident. With source context, hedged-but-
   accurate beats confident-but-wrong. Warn-only.

Every scorer returns the same shape so the runner can iterate uniformly.
ScoreResult.score is float | None; None means N/A (zero-citation answer,
excluded from mean per ADR 0012 D1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from .dataset import GoldenCase


@dataclass(frozen=True)
class ScoreResult:
    scorer: str
    score: float | None          # None = N/A — excluded from mean (ADR 0012 D1)
    passed: bool                 # against this scorer's own threshold
    detail: dict                 # arbitrary scorer-specific detail for the report


@dataclass(frozen=True)
class AgentResponse:
    """What the agent under test returns. Mirror the FastAPI response shape."""

    answer_text: str
    citations: list[dict]        # [{"guidance_id": str, "chunk_index": int}] — raw parsed pairs
    retrieved_passages: list[dict]  # [{"guidance_id": str, "text": str, ...}]
    raw_trace_id: str | None = None


class Scorer(Protocol):
    name: str
    threshold: float
    gate: bool   # True → blocks build on fail; False → warn only

    def score(self, case: GoldenCase, response: AgentResponse) -> ScoreResult: ...


# ─── Scorer 1: Citation validity (deterministic, HARD GATE) ─────────────────

class CitationValidityScorer:
    """For every cited (guidance_id, chunk_index) pair, verify the key exists in
    corpus.chunks via check_citation key-existence mode (ADR 0010 Day 6 baseline).
    Returns the fraction of valid citations.

    Zero-citation answers return score=None (N/A) — excluded from mean, never 0.0
    (ADR 0012 D1). The runner emits a separate zero-citation count in every report.
    """

    name = "citation_validity"
    threshold = 0.95
    gate = True

    def __init__(self, resolves):
        # resolves(guidance_id, chunk_index) -> bool
        # Backed by check_citation key-existence mode; True iff corpus row exists.
        self._resolves = resolves

    def score(self, case: GoldenCase, response: AgentResponse) -> ScoreResult:
        if not response.citations:
            # N/A sentinel — correct refusal answers may legitimately cite nothing.
            # Never scored 0.0; excluded from mean by the runner (ADR 0012 D1).
            return ScoreResult(
                self.name,
                None,
                False,
                {"reason": "zero citations — N/A, excluded from mean (ADR 0012 D1)"},
            )

        valid = 0
        invalid_details = []
        for c in response.citations:
            if self._resolves(c["guidance_id"], c["chunk_index"]):
                valid += 1
            else:
                invalid_details.append({"citation": c, "reason": "key not found in corpus"})

        score = valid / len(response.citations)
        return ScoreResult(
            self.name,
            score,
            score >= self.threshold,
            {"valid": valid, "total": len(response.citations), "invalid": invalid_details},
        )


# ─── Scorer 2: Key fact coverage (LLM-as-judge, Haiku) ──────────────────────

KEY_FACT_PROMPT = """\
You are grading whether an answer covers a set of expected facts.

EXPECTED FACTS (each is a statement the answer should make or imply):
{facts}

ANSWER:
{answer}

For each expected fact, decide: is it present in the answer (explicitly or as
a clear implication)? Respond with ONLY a JSON object of the form:
{{"present": [true, false, true, ...], "notes": "brief reasoning"}}

The "present" list must have exactly {n} booleans, one per fact in order.
"""


class KeyFactCoverageScorer:
    name = "key_fact_coverage"
    threshold = 0.80
    gate = False  # warn only

    def __init__(self, judge_client, model: str):
        self._judge = judge_client
        self._model = model

    def score(self, case: GoldenCase, response: AgentResponse) -> ScoreResult:
        facts = case.expected_facts
        if not facts:
            return ScoreResult(self.name, 1.0, True, {"reason": "no expected facts"})

        prompt = KEY_FACT_PROMPT.format(
            facts="\n".join(f"{i+1}. {f}" for i, f in enumerate(facts)),
            answer=response.answer_text,
            n=len(facts),
        )

        parsed = None
        for attempt in range(2):
            raw = self._judge(self._model, prompt)
            try:
                parsed = json.loads(raw)
                break
            except json.JSONDecodeError:
                if attempt == 1:
                    return ScoreResult(
                        self.name,
                        None,
                        False,
                        {"reason": "judge returned non-JSON after 2 attempts", "raw": raw[:200]},
                    )

        present = parsed["present"]
        score = sum(present) / len(present)
        return ScoreResult(
            self.name,
            score,
            score >= self.threshold,
            {"present": present, "notes": parsed.get("notes", ""), "facts": list(facts)},
        )


# ─── Scorer 3: Position quality (LLM-as-judge with source context, Sonnet) ──

POSITION_QUALITY_PROMPT = """\
You are an expert reviewer grading the quality of a regulatory analysis.

You have access to the SOURCE PASSAGES the analyst retrieved. Use them as
the ground truth. An answer that sounds confident but is not supported by
the source passages should score LOW. An answer that hedges appropriately
when the source is unclear should score HIGH.

USER QUERY:
{query}

PRODUCT CONTEXT:
{product_context}

SOURCE PASSAGES THE ANALYST HAD ACCESS TO:
{passages}

ANALYST'S ANSWER:
{answer}

Score on a 1-5 scale:
  5 = Accurate, well-grounded in sources, appropriately hedged where sources are unclear
  4 = Mostly accurate and grounded, minor issues
  3 = Partially correct, some claims not grounded in sources
  2 = Significant ungrounded claims or material errors
  1 = Largely unsupported by the sources or fundamentally wrong

Respond with ONLY a JSON object: {{"score": <1-5 integer>, "reasoning": "..."}}
"""


class PositionQualityScorer:
    name = "position_quality"
    threshold = 4.0   # on the 1-5 raw scale; passed check uses raw score
    gate = False

    def __init__(self, judge_client, model: str):
        self._judge = judge_client
        self._model = model

    def score(self, case: GoldenCase, response: AgentResponse) -> ScoreResult:
        passages_text = "\n\n---\n\n".join(
            f"[{p['guidance_id']}] {p['text']}" for p in response.retrieved_passages
        )
        prompt = POSITION_QUALITY_PROMPT.format(
            query=case.query,
            product_context=case.product_context or "(none provided)",
            passages=passages_text or "(no passages retrieved)",
            answer=response.answer_text,
        )

        parsed = None
        for attempt in range(2):
            raw = self._judge(self._model, prompt)
            try:
                parsed = json.loads(raw)
                break
            except json.JSONDecodeError:
                if attempt == 1:
                    return ScoreResult(
                        self.name,
                        None,
                        False,
                        {"reason": "judge returned non-JSON after 2 attempts", "raw": raw[:200]},
                    )

        raw_score = int(parsed["score"])   # 1-5 integer
        # passed uses raw 1-5 scale (threshold=4.0); stored score is normalized to 0-1
        passed = raw_score >= self.threshold
        normalized = raw_score / 5.0
        return ScoreResult(
            self.name,
            normalized,
            passed,
            {"raw_score": raw_score, "reasoning": parsed.get("reasoning", "")},
        )
