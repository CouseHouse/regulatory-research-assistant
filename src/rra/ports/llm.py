"""LLM port: stable protocol that agent logic targets.

Design note (ADR 0003):
  The agents speak the Anthropic Messages API directly.  That API surface is
  itself cloud-portable — anthropic ships ``Anthropic``, ``AnthropicBedrock``,
  and ``AnthropicVertex`` with an identical ``.messages.create`` surface.  So
  this port abstracts CLIENT CONSTRUCTION + the create call, not message
  shapes.  ``anthropic.types.Message`` (and all other anthropic types) remain
  the port's wire types by design; agent code keeps its existing type imports.

  For the local profile the adapter wraps a plain ``anthropic.Anthropic``
  client.  For aws/azure/gcp the same port will be wired to
  ``AnthropicBedrock`` / ``AnthropicVertex`` in the cloud-adapter phase.

Factory:
  ``get_llm()`` is the only entry point.  It is profile-resolved via
  ``settings.rra_profile`` and lru_cache'd to a process-lifetime singleton.
  Non-local profiles raise ``NotImplementedError`` until the cloud-adapter
  phase lands.

Singleton behaviour:
  Previously each agent built a fresh ``Anthropic(api_key=...)`` client per
  call.  ``get_llm()`` returns one shared adapter for the process lifetime.
  The anthropic client is thread-safe (identical to the existing
  ``_voyage_client()`` singleton pattern in retrieval.py), so this is the
  only observable behaviour delta of this port introduction.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol

from anthropic.types import Message

from rra.config import settings


class LLMPort(Protocol):
    """Protocol for LLM completion.

    The single method ``complete`` maps directly onto
    ``anthropic.Anthropic.messages.create``.  All kwargs are forwarded
    unchanged so callers retain the full Anthropic Messages API surface.
    """

    def complete(self, **kwargs: Any) -> Message:
        """Call the LLM and return an Anthropic Message.

        All keyword arguments are forwarded verbatim to the underlying
        provider client (model, max_tokens, system, messages, tools, …).
        """
        ...  # pragma: no cover


@lru_cache(maxsize=1)
def get_llm() -> LLMPort:
    """Return the profile-resolved LLM adapter (process-lifetime singleton).

    Adapter modules are imported lazily here so that future cloud adapters
    (boto3, google-cloud-aiplatform, …) are not imported in the local profile.
    """
    profile = settings.rra_profile

    if profile == "local":
        from rra.adapters.anthropic_llm import AnthropicLLMAdapter  # lazy

        return AnthropicLLMAdapter()

    raise NotImplementedError(
        f"{profile!r} adapter lands in the cloud-adapter phase"
    )
