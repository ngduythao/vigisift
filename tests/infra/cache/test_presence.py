import asyncio

from redis.asyncio import Redis

from vigisift.infra.cache.presence import RedisPresence


async def test_a_heartbeat_makes_a_device_online(redis: Redis) -> None:
    presence = RedisPresence(redis, ttl_sec=30)

    await presence.touch("lamp-1")

    assert await presence.online() == {"lamp-1"}


async def test_silence_longer_than_the_ttl_means_offline(redis: Redis) -> None:
    presence = RedisPresence(redis, ttl_sec=0.2)
    await presence.touch("lamp-1")

    await asyncio.sleep(0.4)

    assert await presence.online() == set(), "no recent heartbeat means no longer online"


async def test_forget_removes_a_device_at_once(redis: Redis) -> None:
    presence = RedisPresence(redis, ttl_sec=30)
    await presence.touch("lamp-1")

    await presence.forget("lamp-1")

    assert await presence.online() == set()
