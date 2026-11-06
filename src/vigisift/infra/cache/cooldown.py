from redis.asyncio import Redis


class RedisCooldown:
    """Claim the right to do something costly, at most once every `seconds`.

    `SET key 1 NX EX seconds` is one atomic command: when two motion events arrive in
    the same millisecond, exactly one wins. The old way read `max(created_at)` from
    Postgres before deciding, so both sides saw "nobody has described this" and both
    called the VLM.

    This prevents waste; it doesn't protect an invariant. If Redis loses the lock, the
    worst case is one extra model call. Whatever must be strictly correct stays in
    Postgres constraints.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def acquire(self, key: str, seconds: int) -> bool:
        return bool(await self._redis.set(f"cooldown:{key}", "1", nx=True, ex=seconds))
