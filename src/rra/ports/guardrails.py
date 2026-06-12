"""Guardrails/policy port: stable protocol for content safety checks.

Design note (ADR 0022):
  Every untrusted string that enters the system — either from the user or from
  retrieved corpus content — MUST pass through this port before reaching the
  agent graph or being included in context.

  Two boundaries are enforced:
    1. ``user_input`` boundary (api.py /query): the incoming query and
       product_context are checked before the graph is started.
    2. ``retrieved_content`` boundary (researcher.py): each passage returned
       from search_corpus is checked before it is passed downstream.  This is
       the primary indirect-injection control.

  The ``AllowAllGuardrails`` adapter (allowall_guardrails.py) is the phase-2
  wiring adapter: it always allows and produces no observable behaviour change
  vs. today.  The security-harness phase swaps it for a real detector (LLM Guard
  for the local profile; Bedrock Guardrails / Azure AI Content Safety / Vertex
  Model Armor for cloud profiles).

Verdict contract:
  - ``allowed=True`` — the text is safe to proceed.
  - ``allowed=False`` — the text triggered a policy.  The caller MUST block the
    request and MUST NOT echo any part of the offending text in error responses.
  - ``boundary`` — which boundary produced this verdict ("user_input" or
    "retrieved_content").
  - ``categories`` — tuple of category labels that fired (empty when allowed or
    when the adapter does not produce categories).
  - ``score`` — detector confidence score if available, None otherwise.
  - ``reason`` — short machine-ish label (NOT the raw text); None if not
    available.

Security invariants:
  - The raw text is NEVER logged; only boundary, categories, and score appear
    in structured log events.
  - Blocked requests return a generic HTTP 400 with detail
    "request blocked by content policy" — no echoing of the query, no hint
    about which category fired.
  - Blocked passages are silently dropped; guidance_id + chunk_index are logged
    (``guardrails.passage_blocked``) but the text is not.

Factory:
  ``get_guardrails()`` is the only entry point.  Profile-resolved and
  ``lru_cache``'d to a process-lifetime singleton.  Non-local profiles raise
  ``NotImplementedError`` until the cloud-adapter phase lands.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal, Protocol

from rra.config import settings


# ─── Verdict data type ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GuardrailVerdict:
    """Result of a single guardrails check.

    Attributes:
        allowed:    True iff the text is safe to proceed.
        boundary:   Which boundary was checked ("user_input" or
                    "retrieved_content").
        categories: Tuple of policy-category labels that fired.  Empty when
                    allowed or when the adapter does not produce categories.
        score:      Detector confidence (0.0–1.0) if available, None otherwise.
        reason:     Short machine-ish label for why the text was blocked.  NEVER
                    the raw text.  None if not applicable.
    """

    allowed: bool
    boundary: Literal["user_input", "retrieved_content"]
    categories: tuple[str, ...] = field(default_factory=tuple)
    score: float | None = None
    reason: str | None = None


# ─── Port Protocol ────────────────────────────────────────────────────────────


class GuardrailsPort(Protocol):
    """Protocol for content safety checks.

    The single method ``check`` accepts a text string and a boundary label and
    returns a GuardrailVerdict.  Callers are responsible for acting on the
    verdict (block or pass through).

    Security contract:
      - The implementation MUST NOT log the raw text.
      - The implementation MUST NOT raise on expected policy verdicts; only
        infrastructure failures should raise.
    """

    def check(
        self,
        text: str,
        *,
        boundary: Literal["user_input", "retrieved_content"],
    ) -> GuardrailVerdict:
        """Check *text* against content policy.

        Args:
            text:     The string to check.  MUST NOT be logged by the
                      implementation.
            boundary: Which processing boundary this check is at.

        Returns:
            GuardrailVerdict with ``allowed`` True or False.
        """
        ...  # pragma: no cover


# ─── Factory ──────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_guardrails() -> GuardrailsPort:
    """Return the profile-resolved guardrails adapter (process-lifetime singleton).

    Resolution:
      - "allowall" → AllowAllGuardrails (unconditional pass; tests and explicit
                     cloud stubs).  Valid in ANY profile.
      - "local-hf" → HFInjectionGuardrails (DeBERTa CPU; the secure default for
                     the local profile per CLAUDE.md / ADR 0022).  Valid ONLY in
                     the local profile.
      - Any non-local profile that has NOT explicitly chosen "allowall" raises
        NotImplementedError: the cloud-native adapter (Bedrock Guardrails /
        Model Armor / Content Safety) lands in the cloud-adapter phase.  This
        guard exists because `guardrail*` fields carry a local field default
        (local-hf); without it, RRA_PROFILE=aws would SILENTLY run the local HF
        detector as if it were the cloud guardrail (SC Phase 3 Finding B).
    """
    detector = settings.guardrails_detector
    profile = settings.rra_profile

    if detector == "allowall":
        from rra.adapters.allowall_guardrails import AllowAllGuardrails

        return AllowAllGuardrails()

    # Beyond this point the only built adapter is the local HF detector.  A
    # non-local profile must not fall through to it via the field default.
    if profile != "local":
        raise NotImplementedError(
            f"guardrails adapter for profile={profile!r} lands in the "
            f"cloud-adapter phase; set GUARDRAILS_DETECTOR=allowall to stub it"
        )

    if detector == "local-hf":
        from rra.adapters.hf_injection_guardrails import HFInjectionGuardrails

        return HFInjectionGuardrails(
            model_name=settings.guardrail_model,
            threshold=settings.guardrail_threshold,
            revision=settings.guardrail_model_revision,
        )

    raise NotImplementedError(
        f"unknown guardrails detector {detector!r} for profile={profile!r}"
    )
