import asyncio

import anthropic
import httpx2 as httpx
import pytest

from vigisift.infra.llm.retry import with_retry


def _status_error(cls: type[anthropic.APIStatusError], status: int, **headers: str) -> Exception:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request, headers=headers)
    return cls("error", response=response, body=None)


async def _no_sleep(_: float) -> None:
    return None


def test_overloaded_529_is_retried() -> None:
    attempts: list[int] = []

    async def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise _status_error(anthropic.OverloadedError, 529)
        return "ok"

    assert asyncio.run(with_retry(flaky, sleep=_no_sleep)) == "ok"
    assert len(attempts) == 3


def test_retry_after_is_honoured() -> None:
    waited: list[float] = []
    attempts: list[int] = []

    async def record(seconds: float) -> None:
        waited.append(seconds)

    async def limited() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise _status_error(anthropic.RateLimitError, 429, **{"retry-after": "7"})
        return "ok"

    asyncio.run(with_retry(limited, sleep=record))

    assert waited == [7.0]


def test_a_bad_request_is_never_retried() -> None:
    attempts: list[int] = []

    async def broken() -> str:
        attempts.append(1)
        raise _status_error(anthropic.BadRequestError, 400)

    with pytest.raises(anthropic.BadRequestError):
        asyncio.run(with_retry(broken, sleep=_no_sleep))
    assert len(attempts) == 1, "retrying a bad request only costs more"


def test_retries_stop_after_the_last_attempt() -> None:
    attempts: list[int] = []

    async def down() -> str:
        attempts.append(1)
        raise _status_error(anthropic.OverloadedError, 529)

    with pytest.raises(anthropic.OverloadedError):
        asyncio.run(with_retry(down, attempts=4, sleep=_no_sleep))
    assert len(attempts) == 4
