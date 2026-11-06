from datetime import UTC, datetime

from redis.asyncio import Redis


class RedisTokenBudget:
    """One counter per day (UTC), expiring after 2 days.

    Checking before adding can overshoot the ceiling slightly when calls run in
    parallel; that's acceptable for a cost ceiling, and it avoids a lock. The exact
    numbers for billing are in the llm_calls table in Postgres.
    """

    def __init__(self, redis: Redis, daily_limit: int) -> None:
        self._redis = redis
        self._limit = daily_limit

    def _key(self) -> str:
        return f"llm:tokens:{datetime.now(UTC):%Y-%m-%d}"

    async def remaining(self) -> int:
        used = await self._redis.get(self._key())
        return max(self._limit - int(used or 0), 0)

    async def spend(self, tokens: int) -> None:
        key = self._key()
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incrby(key, tokens)
            pipe.expire(key, 2 * 24 * 3600)
            await pipe.execute()
