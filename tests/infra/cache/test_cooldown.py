import asyncio

from redis.asyncio import Redis

from vigisift.infra.cache.cooldown import RedisCooldown


async def test_twenty_simultaneous_events_win_the_cooldown_once(redis: Redis) -> None:
    cooldown = RedisCooldown(redis)

    wins = await asyncio.gather(*(cooldown.acquire("describe:cam-1", 60) for _ in range(20)))

    assert sum(wins) == 1, "SET NX is atomic: exactly one event may call the VLM"


async def test_cooldowns_are_per_camera(redis: Redis) -> None:
    cooldown = RedisCooldown(redis)

    assert await cooldown.acquire("describe:cam-1", 60)
    assert await cooldown.acquire("describe:cam-2", 60)
