"""Provider factory + per-stage routing.

With LiteLLM there is a single provider — all model routing is handled
internally by the model prefix (e.g. ``anthropic/claude-sonnet-4-6``,
``openai/gpt-4o``). Per-stage overrides select the model; fallback
retries with a different model.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Awaitable, Callable, TypeVar

from backend.config import settings
from backend.services.llm.base import LLMProvider

log = logging.getLogger(__name__)

T = TypeVar("T")


@lru_cache(maxsize=1)
def get_provider_by_name(name: str) -> LLMProvider:
    """Return a singleton provider instance.

    With LiteLLM there is only one provider; ``name`` is accepted for
    backward compat but ignored.
    """
    if name not in ("litellm", "anthropic", "gemini"):
        raise ValueError(f"Unknown LLM provider: {name!r}")
    from backend.services.llm.litellm_provider import LiteLLMProvider
    return LiteLLMProvider()


# Backwards-compatible alias
_get_provider_by_name = get_provider_by_name


def get_provider(stage: str) -> LLMProvider:
    """Return the provider configured for ``stage``.

    With LiteLLM this always returns the same singleton. Kept for
    backward compat with call sites that use the provider abstraction.
    """
    name = settings.provider_for_stage(stage)
    return get_provider_by_name(name)


async def call_with_fallback(
    stage: str,
    body: Callable[[LLMProvider, str], Awaitable[T]],
) -> T:
    """Run ``body(provider, model)`` for ``stage``; on any exception,
    retry once with the fallback model if one is configured via
    ``FALLBACK_MODEL_<STAGE>``.

    The fallback runs ``body`` from scratch — any tokens spent in the
    primary attempt are lost (and not logged). ``asyncio.CancelledError``
    is always re-raised so cancellation still works.
    """
    primary_provider = get_provider(stage)
    primary_model = settings.model_for_stage(stage)
    try:
        return await body(primary_provider, primary_model)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        fb_model = settings.fallback_for_stage(stage)
        if fb_model is None:
            raise
        log.warning(
            "[%s] primary %s/%s failed (%s) — falling back to %s",
            stage, primary_provider.name, primary_model,
            exc, fb_model,
        )
        return await body(primary_provider, fb_model)
