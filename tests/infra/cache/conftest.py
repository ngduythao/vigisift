from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from tests.conftest import REDIS_URL


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()
