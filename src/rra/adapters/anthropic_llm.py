"""Anthropic LLM adapter for the local profile.

Wraps a single ``anthropic.Anthropic`` client and delegates every
``complete(**kwargs)`` call to ``client.messages.create(**kwargs)``.

The client is constructed once at adapter instantiation time and reused for
the lifetime of the process.  This matches the existing ``_voyage_client()``
singleton pattern in retrieval.py and is safe because the anthropic client
is thread-safe.
"""
from __future__ import annotations

from typing import Any

from anthropic import Anthropic
from anthropic.types import Message

from rra.config import settings


class AnthropicLLMAdapter:
    """LLMPort implementation backed by ``anthropic.Anthropic``."""

    def __init__(self) -> None:
        self._client = Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value()
        )

    def complete(self, **kwargs: Any) -> Message:
        """Forward all kwargs to ``client.messages.create`` unchanged.

        We never pass stream=True, so the return is always Message (not a
        streaming iterator).  The cast is necessary because mypy sees the
        overloaded return type and cannot narrow without the literal kwarg.
        """
        result: Message = self._client.messages.create(**kwargs)
        return result
