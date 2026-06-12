"""HuggingFace-based injection-detection guardrails adapter (local profile, Phase 3).

Uses protectai/deberta-v3-base-prompt-injection-v2 (Apache 2.0, ungated, ~184M
DeBERTa params) via the transformers text-classification pipeline on CPU.

Design decisions:
- Lazy model load on first check() call — import time stays fast; torch doesn't
  load until the adapter is actually used.
- Long-passage chunking: the model's max token length is 512 tokens (~2000 chars).
  Passages longer than the model window are split into ≤512-token chunks and ALL
  chunks are evaluated; the maximum injection score across all chunks is used.
  This prevents a payload appended to the END of a long passage from being
  silently truncated away, which matters because the corpus has end-of-passage
  payloads (documented in RT-redteam.md RT-1).
- Security invariant: the raw text is NEVER included in log events, error
  messages, or GuardrailVerdict.reason.  Only the score, boundary, and label
  appear in structured log output (see ADR 0022 / CLAUDE.md).

Logging:
  Only boundary, chars, score, and verdict appear at DEBUG/INFO level.  The text
  is NEVER logged — not even a truncated prefix.  This invariant must be
  preserved in all future modifications.
"""
from __future__ import annotations

from typing import Any

import structlog

from rra.ports.guardrails import GuardrailVerdict

log = structlog.get_logger(__name__)

# Characters per model token.  DeBERTa-v3 BPE on dense regulatory English with
# device names, codes (510(k), PCCP), and URLs tokenizes well below 4 chars/token;
# RT review (RT-P3-2) showed 4 was optimistic enough that the pipeline's own
# truncation could drop an end-of-passage payload from a "single" chunk BEFORE
# scoring.  3 keeps a real 512-token window inside one chunk so truncation never
# eats a payload the chunker thought it kept.
_CHARS_PER_TOKEN: int = 3
# Model hard limit in tokens; the DeBERTa-v3 family uses 512.
_MODEL_MAX_TOKENS: int = 512
# Soft character budget per chunk — leaves headroom for the [CLS]/[SEP] special
# tokens the tokenizer prepends/appends.
_CHUNK_CHARS: int = (_MODEL_MAX_TOKENS - 2) * _CHARS_PER_TOKEN  # ≈ 1530
# Overlap between consecutive chunks.  A payload straddling a chunk boundary
# would otherwise be split so each half scores below threshold (RT-P3-2
# dilution evasion); the overlap guarantees a payload shorter than the overlap
# appears intact in at least one window, so max-of-chunks sees it.
_CHUNK_OVERLAP: int = _CHUNK_CHARS // 4  # ≈ 382 chars


def _chunk_text(text: str) -> list[str]:
    """Split *text* into overlapping chunks of at most _CHUNK_CHARS characters.

    Character-level split with a sliding overlap so a payload straddling a chunk
    boundary still appears intact in one window.  The model accepts any UTF-8
    text; oversized chunks are still truncated by the pipeline's own tokenizer
    as a final guard (the two are belt-and-suspenders now that the char budget
    is sized to fit a real 512-token window).
    """
    if len(text) <= _CHUNK_CHARS:
        return [text]
    chunks: list[str] = []
    start = 0
    stride = _CHUNK_CHARS - _CHUNK_OVERLAP
    while start < len(text):
        chunks.append(text[start : start + _CHUNK_CHARS])
        if start + _CHUNK_CHARS >= len(text):
            break
        start += stride
    return chunks


class HFInjectionGuardrails:
    """GuardrailsPort adapter backed by a HuggingFace text-classification pipeline.

    The model (protectai/deberta-v3-base-prompt-injection-v2) emits a binary
    INJECTION / SAFE label with a 0–1 confidence score.  If ANY chunk of the
    input text scores ≥ guardrail_threshold on the INJECTION label, the verdict
    is blocked.

    Thread safety: the pipeline object is not re-entrant; production use is
    single-process (FastAPI + LangGraph run synchronously); concurrent calls
    must use separate instances or serialize access.
    """

    def __init__(
        self, model_name: str, threshold: float, revision: str | None = None
    ) -> None:
        self._model_name = model_name
        self._threshold = threshold
        # Pin the model to an exact Hub commit (RT-8 supply chain): the Python
        # lockfile pins packages, NOT model weights — they are a separate supply
        # chain.  A bare repo id resolves to whatever `main` points at, so a
        # repo-owner compromise or re-push would swap weights with no signal.
        self._revision = revision
        # Lazy-loaded on first check().  Typed Any because the transformers
        # pipeline type only exists after the lazy import.
        self._pipeline: Any = None

    def _load_pipeline(self) -> None:
        """Lazy-load the transformers pipeline on first use.

        Deferred so that importing the adapter (e.g., at config time) never
        triggers a torch import — torch is only loaded when check() is
        actually called.
        """
        if self._pipeline is not None:
            return

        # Import transformers lazily; never at module load time.  This keeps
        # the import graph clean for the 359 unit tests that use GUARDRAILS_DETECTOR=allowall
        # and never need torch (see conftest.py).
        from transformers import pipeline

        log.info(
            "guardrails.hf.loading_model",
            model=self._model_name,
            revision=self._revision,
        )
        self._pipeline = pipeline(
            "text-classification",
            model=self._model_name,
            revision=self._revision,  # RT-8: exact-commit pin (None only if unset)
            device=-1,  # CPU; -1 is the transformers convention for "no GPU"
            truncation=True,
            max_length=_MODEL_MAX_TOKENS,
            # trust_remote_code=False is the default; listed explicitly per RT-8
            # supply-chain control: refuse models that require arbitrary remote code.
            trust_remote_code=False,
            # Prefer safetensors over pickle-based weight loading (RT-8): no
            # arbitrary code executes at load time.
            use_safetensors=True,
        )
        log.info("guardrails.hf.model_loaded", model=self._model_name)

    def _score_chunk(self, chunk: str) -> float | None:
        """Return the INJECTION confidence for a chunk, or None on malfunction.

        None signals the detector is not running as expected (empty result or an
        unrecognized label — a model swap, corrupted download, or RT-8 weight
        tampering).  The caller treats None as fail-CLOSED (block), not fail-open:
        a non-functioning injection detector at a deny-by-default boundary must
        block, consistent with the threshold-0.2 rationale (uncertainty resolves
        toward blocking; the cost of a block is one dropped passage).
        """
        self._load_pipeline()
        assert self._pipeline is not None  # type narrowing

        # The pipeline returns a list of dicts: [{"label": "...", "score": float}]
        # We call with a list of one item and get back a list of one item.
        results: list[dict[str, object]] = self._pipeline([chunk])
        if not results:
            log.warning("guardrails.hf.empty_result")
            return None
        result = results[0]
        label = str(result.get("label", "")).upper()
        raw_score = result.get("score", 0.0)
        score = float(raw_score) if isinstance(raw_score, (int, float)) else 0.0
        # The model emits either "INJECTION" or "SAFE".  If it emits INJECTION,
        # the score is the injection confidence directly.  If it emits SAFE, the
        # injection confidence is 1.0 - score (the complementary probability).
        if label == "INJECTION":
            return score
        if label == "SAFE":
            return 1.0 - score
        # Unexpected label — the detector is malfunctioning.  Fail CLOSED.
        log.warning("guardrails.hf.unexpected_label", label=label)
        return None

    def check(
        self,
        text: str,
        *,
        boundary: str,
    ) -> GuardrailVerdict:
        """Check *text* for prompt injection.

        Chunks the text to handle passages longer than the model's 512-token
        window, then takes the maximum injection score across all chunks.  This
        means a payload appended to the end of a long passage is detected even
        if the first chunk is benign.

        Security invariants:
          - The raw text is NEVER logged.
          - The reason field of the verdict is a machine-readable score label,
            not the input text.
        """
        from typing import Literal

        boundary_typed: Literal["user_input", "retrieved_content"] = (
            "user_input" if boundary == "user_input" else "retrieved_content"
        )

        log.debug("guardrails.hf.check", boundary=boundary_typed, chars=len(text))

        chunks = _chunk_text(text)
        max_score: float = 0.0
        malfunction = False
        for chunk in chunks:
            score = self._score_chunk(chunk)
            if score is None:
                malfunction = True
                continue
            if score > max_score:
                max_score = score

        # Fail CLOSED: if the detector malfunctioned on any chunk, block —
        # a non-functioning detector must not silently admit content.
        blocked = malfunction or max_score >= self._threshold

        log.debug(
            "guardrails.hf.verdict",
            boundary=boundary_typed,
            score=round(max_score, 4),
            blocked=blocked,
            malfunction=malfunction,
            chunks=len(chunks),
        )

        if blocked:
            return GuardrailVerdict(
                allowed=False,
                boundary=boundary_typed,
                categories=("prompt_injection",),
                score=max_score,
                # reason is a machine-readable label only — NEVER the raw text.
                reason=(
                    "detector_malfunction_fail_closed"
                    if malfunction
                    else "injection_score_above_threshold"
                ),
            )

        return GuardrailVerdict(
            allowed=True,
            boundary=boundary_typed,
            score=max_score,
        )
