import asyncio
import json

from redis.asyncio import Redis

from vigisift.features.telemetry.hub import TelemetryHub
from vigisift.infra.cache.events import RedisEventBus


async def test_one_publish_reaches_every_instance(redis: Redis) -> None:
    hubs = [TelemetryHub(), TelemetryHub()]
    buses = [RedisEventBus(redis, hub.publish, "vigisift:test-fanout") for hub in hubs]
    listeners = [asyncio.create_task(bus.run()) for bus in buses]
    for bus in buses:
        await bus.wait_ready()

    with hubs[0].subscribe() as first, hubs[1].subscribe() as second:
        await buses[1].publish({"text": "hello"})
        received = await asyncio.wait_for(asyncio.gather(first.get(), second.get()), timeout=2)
    for task in listeners:
        task.cancel()

    assert [json.loads(m) for m in received] == [{"text": "hello"}] * 2
