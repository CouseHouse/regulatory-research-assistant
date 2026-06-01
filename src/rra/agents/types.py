"""Internal types shared across graph agent nodes.

These are graph-internal and never serialized into QueryResponse.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CriticNote(BaseModel):
    """A single issue identified by the critic node."""

    citation_key: str | None  # "guidance_id:chunk_index"; None for claim-level issues
    issue: str
    severity: Literal["hard", "soft"]
    # hard = analyst must address; soft = suggestion only


class CriticOutput(BaseModel):
    """Structured output returned by the critic LLM call."""

    verdict: Literal["approve", "revise", "escalate"]
    notes: list[CriticNote]
