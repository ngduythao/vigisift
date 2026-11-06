import time

from redis.asyncio import Redis

_KEY = "presence"


class RedisPresence:
    """Sorted set: the member is the device id, the score is when it was last seen.

    One key for the whole home instead of one TTL key per device: asking "who is
    online" is one O(log N + M) ZRANGEBYSCORE, not a SCAN of the keyspace.
    """

    def __init__(self, redis: Redis, ttl_sec: float) -> None:
        self._redis = redis
        self._ttl = ttl_sec

    async def touch(self, device_id: str) -> None:
        await self._redis.zadd(_KEY, {device_id: time.time()})

    async def forget(self, device_id: str) -> None:
        await self._redis.zrem(_KEY, device_id)

    async def online(self) -> set[str]:
        cutoff = time.time() - self._ttl
        # Clear out devices silent for too long so the set doesn't grow forever.
        await self._redis.zremrangebyscore(_KEY, "-inf", cutoff)
        members = await self._redis.zrangebyscore(_KEY, cutoff, "+inf")
        return {str(m) for m in members}
