import asyncio
import logging
import os
import socket
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion

from vigisift.ports.messaging import Handler

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Subscription:
    topic: str
    handler: Handler
    shared_group: str | None
    loop: asyncio.AbstractEventLoop

    @property
    def wire_topic(self) -> str:
        # Shared subscription: the broker gives each message to exactly ONE member
        # of the group. With 2 API instances, each telemetry message is stored
        # once rather than once per instance.
        if self.shared_group:
            return f"$share/{self.shared_group}/{self.topic}"
        return self.topic


def dispatch(subscriptions: list[Subscription], topic: str, payload: str) -> None:
    """Run the handler matching the topic on the event loop that registered it.

    paho calls back from its own thread, not the event loop, so it can't await
    directly; the coroutine has to be sent back to the loop.
    """
    for subscription in subscriptions:
        # The broker returns the real topic (device/lamp-1/telemetry), without the
        # $share prefix, so match against the original topic.
        if paho.topic_matches_sub(subscription.topic, topic):
            future = asyncio.run_coroutine_threadsafe(
                subscription.handler(topic, payload), subscription.loop
            )
            future.add_done_callback(_report_failure(topic))


def _report_failure(topic: str) -> "Callable[[Future[None]], None]":
    """A handler that raises (the DB going down, say) leaves the error in a future nobody reads.

    Without this callback the message is lost and the log has not one line about it.
    """

    def report(future: "Future[None]") -> None:
        if not future.cancelled() and (error := future.exception()) is not None:
            log.error("handler for %s failed", topic, exc_info=error)

    return report


class PahoMqttClient:
    def __init__(
        self,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
        client_id: str | None = None,
    ) -> None:
        # client_id must be unique: the broker kicks the old connection when a new one uses
        # the same id, and two instances sharing an id keep kicking each other.
        self._client = paho.Client(
            CallbackAPIVersion.VERSION2,
            client_id=client_id or f"vigisift-{socket.gethostname()}-{os.getpid()}",
        )
        if username:
            self._client.username_pw_set(username, password)
        self._subscriptions: list[Subscription] = []
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect(host, port, keepalive=30)
        self._client.loop_start()

    def _on_connect(
        self, client: paho.Client, _u: object, _f: object, reason: Any, _p: object
    ) -> None:
        # After a lost connection paho reconnects, but the clean session means the broker
        # forgot the subscriptions. Without resubscribing here the server is "alive" but deaf.
        if reason.is_failure:
            return
        for subscription in self._subscriptions:
            client.subscribe(subscription.wire_topic, qos=1)

    def _on_message(self, _c: object, _u: object, message: paho.MQTTMessage) -> None:
        dispatch(self._subscriptions, message.topic, message.payload.decode())

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def subscribe(self, topic: str, handler: Handler, shared_group: str | None = None) -> None:
        subscription = Subscription(topic, handler, shared_group, asyncio.get_running_loop())
        self._subscriptions.append(subscription)
        self._client.subscribe(subscription.wire_topic, qos=1)

    def close(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()
