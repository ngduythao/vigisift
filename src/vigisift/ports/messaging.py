from collections.abc import Callable, Coroutine
from typing import Any, Protocol

Handler = Callable[[str, str], Coroutine[Any, Any, None]]


class MqttClient(Protocol):
    """The way out to MQTT.

    A handler is a coroutine taking (topic, payload); the adapter runs it on the
    event loop, even when the client library calls back from its own thread.
    """

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None: ...

    def subscribe(self, topic: str, handler: Handler, shared_group: str | None = None) -> None: ...

    def close(self) -> None: ...
