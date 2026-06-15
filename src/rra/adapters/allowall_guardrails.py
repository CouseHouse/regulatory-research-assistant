"""Allow-all guardrails adapter for the local profile (phase-2 wiring).

This adapter always returns GuardrailVerdict(allowed=True) and is the
phase-2 wiring stand-in: it installs the call sites in api.py and
researcher.py without changing observable behaviour vs. today.

The security-harness phase (next phase after port wiring) replaces this adapter
with a real content-safety detector for the local profile:
  - Local: LLM Guard (self-hosted, docker-compose)
Cloud profiles map to their native managed services:
  - aws:   Amazon Bedrock Guardrails
  - azure: Azure AI Content Safety
  - gcp:   Vertex AI Model Armor

Logging:
  Only boundary and content length are logged at DEBUG level.  The text is
  NEVER logged — not even a truncated prefix.  This invariant must be
  preserved when this adapter is replaced.
"""
from __future__ import annotations

from typing import Literal

import structlog

from rra.ports.guardrails import GuardrailVerdict

log = structlog.get_logger(__name__)


class AllowAllGuardrails:
    """GuardrailsPort that unconditionally allows all content.

    This is the phase-2 wiring adapter.  It installs the guardrails call
    sites (api.py /query and researcher.py passage filter) without changing
    the system's observable behaviour.  The local detection adapter (LLM
    Guard) replaces it in the security-harness phase; cloud profiles map to
    Bedrock Guardrails / Azure AI Content Safety / Vertex Model Armor.
    """

    def check(
        self,
        text: str,
        *,
        boundary: Literal["user_input", "retrieved_content"],
    ) -> GuardrailVerdict:
        """Always allow.  Logs boundary and text length at DEBUG; never logs text."""
        log.debug("guardrails.check", boundary=boundary, chars=len(text))
        return GuardrailVerdict(allowed=True, boundary=boundary)
