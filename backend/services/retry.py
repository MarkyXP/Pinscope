"""Generic async retry decorator with exponential back-off and jitter.

Designed for LLM / external-API calls that may raise rate-limit errors.
The decorator honours a ``Retry-After`` header on the exception's
``.response`` attribute when present.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from typing import Any, Callable, Type

log = logging.getLogger(__name__)


def async_retry(
    *,
    exceptions: tuple[Type[BaseException], ...],
    max_retries: int = 6,
    base_delay: float = 5.0,
    cap: float = 120.0,
    jitter: float = 0.1,
    retry_after_attr: str = "response",
) -> Callable:
    """Decorator: retry *func* on *exceptions* with exponential back-off + jitter.

    Args:
        exceptions:        Exception types that should trigger a retry.
        max_retries:       Maximum number of retry attempts (total calls = max_retries + 1).
        base_delay:        Initial delay in seconds (doubles each attempt).
        cap:               Maximum delay ceiling in seconds.
        jitter:            Fractional jitter applied as ±jitter of the computed delay.
        retry_after_attr:  Attribute name on the exception that exposes the HTTP response
                           (used to read the ``Retry-After`` header).
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    if attempt >= max_retries:
                        raise
                    retry_after: float | None = None
                    try:
                        resp = getattr(exc, retry_after_attr, None)
                        if resp is not None:
                            hdr = resp.headers.get("Retry-After")
                            if hdr:
                                retry_after = float(hdr)
                    except Exception:
                        pass
                    delay = min(
                        retry_after or (base_delay * (2**attempt)),
                        cap,
                    )
                    delay *= 1 + random.uniform(-jitter, jitter)
                    log.warning(
                        "%s on attempt %d/%d — retrying in %.1fs",
                        type(exc).__name__,
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)

        return wrapper

    return decorator
