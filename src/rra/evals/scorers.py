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
    # [{"guidance_id": str, "chunk_index": int, "quoted_text": str | None}]
    # quoted_text (ADR 0013): the analyst's span; "" = golden no-quote;
    # None = key-existence mode (CI fixture). A value-shape change only — still
    # list[dict] — so callers building citations=[] are unaffected.
    citations: list[dict]
    retrieved_passages: list[dict]  # [{"guidance_id": str, "text": str, ...}]
    raw_trace_id: str | None = None


class Scorer(Protocol):
    name: str
    threshold: float
    gate: bool   # True → blocks build on fail; False → warn only

    def score(self, case: GoldenCase, response: AgentResponse) -> ScoreResult: ...


# ─── Scorer 1: Citation validity (deterministic, HARD GATE) ─────────────────

class CitationValidityScorer:
    """Verify each cited (guidance_id, chunk_index) via check_citation, in one of
    two modes chosen PER CITATION by its quoted_text (ADR 0013):

      - quoted_text is None  → KEY-EXISTENCE (CI fixture; ADR 0012 D2 / Day-6
        baseline): valid iff the corpus row exists. This is the unchanged HARD
        gate.
      - quoted_text is a non-empty string → QUOTE FAITHFULNESS (golden): valid iff
        the analyst's quote faithfully appears in the chunk (ADR 0010 three-step
        engine). similarity_score is captured for τ-calibration.
      - quoted_text is "" → NO QUOTE (golden): faithfulness cannot be assessed.
        Counted in `no_quote` and EXCLUDED from the mean — never scored faithful.
        This is the ADR 0012 D1 analog: a shrinking denominator must not inflate
        the mean, so write_report surfaces the no-quote count (plan §7-#3/#8).

    Returns the fraction of ASSESSED citations that are valid. score=None (N/A) —
    never 0.0 — when there are zero citations or none are assessable (all
    no-quote); excluded from the mean by the runner (ADR 0012 D1). Faithfulness is
    measured POST-GRAPH and gates no revise loop (ADR 0013 / Sign-off A); the CI
    key-existence gate is unchanged.
    """

    name = "citation_validity"
    threshold = 0.95
    gate = True

    def __init__(self, verify):
        # verify(guidance_id, chunk_index, quoted_text) -> (verified: bool, similarity_score: float | None)
        # Backed by check_citation: None → key-existence, str → ADR-0010 matching.
        self._verify = verify

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
        assessed = 0                  # denominator: citations whose validity we could assess
        no_quote = 0                  # golden no-quote citations (excluded from mean)
        key_existence = False         # True if any citation used key-existence mode (CI)
        faithfulness: list[dict] = [] # per faithfulness-mode citation: {similarity_score, verified}
        invalid: list[dict] = []

        for c in response.citations:
            quoted_text = c["quoted_text"]

            if quoted_text is None:
                # Key-existence mode (CI fixture) — the unchanged Day-6 gate.
                key_existence = True
                verified, _ = self._verify(c["guidance_id"], c["chunk_index"], None)
                assessed += 1
                if verified:
                    valid += 1
                else:
                    invalid.append({"citation": c, "reason": "key not found in corpus"})
                continue

            if not quoted_text.strip():
                # Golden no-quote — faithfulness unassessable. Count it and EXCLUDE
                # from the mean; never score it faithful (ADR 0012 D1 analog).
                no_quote += 1
                continue

            # Golden faithfulness — ADR-0010 three-step quote matching.
            verified, sim = self._verify(c["guidance_id"], c["chunk_index"], quoted_text)
            assessed += 1
            faithfulness.append({"similarity_score": sim, "verified": verified})
            if verified:
                valid += 1
            else:
                invalid.append(
                    {"citation": c, "reason": "quote not faithful", "similarity_score": sim}
                )

        detail = {
            "valid": valid,
            "assessed": assessed,
            "no_quote": no_quote,
            "key_existence": key_existence,
            "faithfulness": faithfulness,
            "invalid": invalid,
        }

        if assessed == 0:
            # Every citation was a no-quote — N/A, like a zero-citation answer
            # (ADR 0012 D1). The no_quote count (surfaced by write_report) is what
            # keeps this from silently hiding analyst quote-degradation.
            detail["reason"] = "no assessable citations (all no-quote) — N/A, excluded from mean"
            return ScoreResult(self.name, None, False, detail)

        score = valid / assessed
        return ScoreResult(self.name, score, score >= self.threshold, detail)


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

        # Prefill the assistant turn with "{" so Haiku is forced to begin its
        # reply with raw JSON instead of prose ("Here is the JSON: ..."), the
        # Day 6 bug that made strict json.loads reject every reply → N/A on all
        # 30 cases. judge_call prepends the "{" back, so `raw` is a complete
        # JSON string. Strictness is unchanged: one retry, then score=None on a
        # second failure (we never silently score 0).
        parsed = None
        for attempt in range(2):
            raw = self._judge(
                self._model, prompt, prefill="{", trace_id=response.raw_trace_id
            )
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
            raw = self._judge(self._model, prompt, trace_id=response.raw_trace_id)
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
