import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

import anthropic

log = logging.getLogger(__name__)

# 429 (rate limit), 5xx including 529 (API overloaded), network errors and timeouts:
# retrying helps. 400/401/403/404 give the same error however many times you retry.
# Listed explicitly: in SDK 1.x, OverloadedError (529), ServiceUnavailableError
# (503) and DeadlineExceededError (504) inherit directly from APIStatusError,
# not from InternalServerError. Catching only InternalServerError misses exactly
# the errors most common when the API is overloaded.
# APITimeoutError is a child of APIConnectionError, so it's already covered.
RETRYABLE: tuple[type[Exception], ...] = (
    anthropic.RateLimitError,
    anthropic.OverloadedError,
    anthropic.ServiceUnavailableError,
    anthropic.DeadlineExceededError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
)


def _retry_after(error: Exception) -> float | None:
    response = getattr(error, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


async def with_retry[T](
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 20.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Exponential backoff + jitter, honouring the `retry-after` header.

    The SDK retries by itself too (max_retries defaults to 2). Here the client is built
    with max_retries=0 so there's only ONE policy, shared by every model call and
    logging each attempt. Two stacked retry layers quietly multiply the calls by 9
    whenever the API is flaky.

    Jitter keeps instances that hit 429 together from retrying in step and hitting
    429 together again.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except RETRYABLE as error:
            if attempt == attempts:
                raise
            delay = _retry_after(error)
            if delay is None:
                delay = min(base_delay * 2 ** (attempt - 1), max_delay)
                delay += random.uniform(0, delay / 2)
            log.warning("transient llm error (%s), retry %d in %.1fs", error, attempt, delay)
            await sleep(delay)
    raise AssertionError("unreachable")
