import asyncio
import contextlib
import json
from collections.abc import Callable, Mapping

from redis.asyncio import Redis

Deliver = Callable[[str], object]


class RedisEventBus:
    """WebSocket fanout across instances.

    The in-memory hub only knows connections on ITS OWN instance. With 2 instances
    behind a load balancer, telemetry handled by instance B never reaches a browser
    connected to instance A. Every instance publishes to one Redis channel and
    subscribes to it, then pushes into its own hub.

    Pub/sub is fire-and-forget: an instance that's restarting misses events. That's
    acceptable for a realtime UI (clients reload state over REST); if nothing may be
    lost, use Redis Streams or Kafka.
    """

    def __init__(self, redis: Redis, deliver: Deliver, channel: str) -> None:
        self._redis = redis
        self._deliver = deliver
        self._channel = channel
        self._ready = asyncio.Event()

    async def publish(self, event: Mapping[str, object]) -> None:
        await self._redis.publish(self._channel, json.dumps(event, ensure_ascii=False))

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def run(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel)
        self._ready.set()
        try:
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    self._deliver(str(message["data"]))
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(self._channel)
                await pubsub.aclose()  # type: ignore[no-untyped-call]
