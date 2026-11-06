from redis.asyncio import Redis


class RedisLoginAttempts:
    """INCR + EXPIRE NX in one transaction: the count rises atomically, and the window
    starts at the first attempt and expires by itself after `window_sec`."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, key: str, window_sec: int) -> int:
        name = f"login-attempt:{key}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(name)
            pipe.expire(name, window_sec, nx=True)
            count, _ = await pipe.execute()
        return int(count)

    async def release(self, key: str) -> None:
        name = f"login-attempt:{key}"
        if await self._redis.decr(name) <= 0:
            await self._redis.delete(name)

    async def reset(self, key: str) -> None:
        await self._redis.delete(f"login-attempt:{key}")
