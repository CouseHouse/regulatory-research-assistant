"""Three independent scorers, run on every response.

Design notes:

1. Citation validity is DETERMINISTIC, not LLM-graded. String match against
   the source corpus. This catches hallucinated citations cleanly and
   doesn't drift between eval runs. It is the only HARD gate (CI blocks
   merges on it).

2. Key fact coverage uses an LLM-as-judge (Haiku). Cheap, fast, runs on
   every change.

3. Position quality uses Sonnet WITH the retrieved passages in the judge's
   context. This is the reward-hacking fix — without source context the
   judge agrees with anything confident. With source context, hedged-but-
   accurate beats confident-but-wrong.

Every scorer returns the same shape so the runner can iterate uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .dataset import GoldenCase


@dataclass(frozen=True)
class ScoreResult:
    scorer: str
    score: float                # 0.0–1.0 (or 0.0–5.0 for likert; normalize in reporting)
    passed: bool                # against this scorer's own threshold
    detail: dict                # arbitrary scorer-specific detail for the report


@dataclass(frozen=True)
class AgentResponse:
    """What the agent under test returns. Mirror the FastAPI response shape."""

    answer_text: str
    citations: list[dict]        # [{"guidance_id": "...", "span": "...", "char_start": ..., "char_end": ...}]
    retrieved_passages: list[dict]  # [{"guidance_id": "...", "text": "...", ...}]
    raw_trace_id: str | None = None


class Scorer(Protocol):
    name: str
    threshold: float
    gate: bool   # True → blocks build on fail; False → warn only

    def score(self, case: GoldenCase, response: AgentResponse) -> ScoreResult: ...


# ─── Scorer 1: Citation validity (deterministic, HARD GATE) ─────────────────

class CitationValidityScorer:
    """For every cited (guidance_id, span) pair, verify the span appears in
    the named guidance. Returns the fraction of valid citations."""

    name = "citation_validity"
    threshold = 0.95
    gate = True

    def __init__(self, corpus_lookup):
        # corpus_lookup(guidance_id) -> full guidance text, or None
        self._lookup = corpus_lookup

    def score(self, case: GoldenCase, response: AgentResponse) -> ScoreResult:
        if not response.citations:
            return ScoreResult(self.name, 0.0, False, {"reason": "no citations"})

        valid = 0
        invalid_details = []
        for c in response.citations:
            text = self._lookup(c["guidance_id"])
            if text is None:
                invalid_details.append({"citation": c, "reason": "unknown guidance_id"})
                continue
            # Exact substring match — strict by design
            if c["span"] in text:
                valid += 1
            else:
                invalid_details.append({"citation": c, "reason": "span not in source"})

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
        # TODO(day 6): actual judge call + JSON parsing with retry on malformed output
        # judgment = self._judge.complete(self._model, prompt)
        # parsed = json.loads(judgment)
        # present = parsed["present"]
        raise NotImplementedError("Wire up judge client on day 6")


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
    threshold = 4.0   # on the 1-5 scale; normalize for reporting
    gate = False

    def __init__(self, judge_client, model: str):
        self._judge = judge_client
        self._model = model

    def score(self, case: GoldenCase, response: AgentResponse) -> ScoreResult:
        passages = "\n\n---\n\n".join(
            f"[{p['guidance_id']}] {p['text']}" for p in response.retrieved_passages
        )
        prompt = POSITION_QUALITY_PROMPT.format(
            query=case.query,
            product_context=case.product_context or "(none provided)",
            passages=passages or "(no passages retrieved)",
            answer=response.answer_text,
        )
        # TODO(day 6): wire up
        raise NotImplementedError("Wire up judge client on day 6")
