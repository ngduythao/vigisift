import asyncio
from collections.abc import Iterator
from contextlib import contextmanager


class TelemetryHub:
    """Where WebSocket connections register for realtime telemetry.

    One bounded queue per connection. When a client reads slowly, messages
    **of that client alone** are dropped, without slowing the whole system; with one
    shared queue a hung browser would stall everyone.
    """

    def __init__(self, queue_size: int = 100) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._queue_size = queue_size

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def publish(self, message: str) -> int:
        delivered = 0
        for queue in self._subscribers:
            try:
                queue.put_nowait(message)
                delivered += 1
            except asyncio.QueueFull:
                continue
        return delivered
