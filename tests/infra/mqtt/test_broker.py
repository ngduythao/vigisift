import queue
import threading
import time
import uuid
from typing import Any

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion

HOST, PORT = "127.0.0.1", 1883


def _client(username: str, password: str, **will: object) -> paho.Client:
    client = paho.Client(CallbackAPIVersion.VERSION2, client_id=f"test-{uuid.uuid4().hex[:8]}")
    client.username_pw_set(username, password)
    if will:
        client.will_set(**will)  # type: ignore[arg-type]
    return client


def _listen(topic: str) -> tuple[paho.Client, "queue.Queue[tuple[str, str]]"]:
    received: queue.Queue[tuple[str, str]] = queue.Queue()
    ready = threading.Event()
    listener = _client("vigisift", "vigisift")
    listener.on_message = lambda _c, _u, m: received.put((m.topic, m.payload.decode()))
    listener.on_subscribe = lambda *_: ready.set()
    listener.connect(HOST, PORT)
    listener.loop_start()
    listener.subscribe(topic, qos=1)
    assert ready.wait(5)
    return listener, received


def _collect_until(
    received: "queue.Queue[tuple[str, str]]", wanted: str, seconds: float
) -> list[str]:
    """Collect every payload received until `wanted` is seen (or time runs out)."""
    seen: list[str] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and wanted not in seen:
        try:
            seen.append(received.get(timeout=0.2)[1])
        except queue.Empty:
            continue
    return seen


def test_a_wrong_password_is_refused() -> None:
    connected = threading.Event()
    outcome: list[bool] = []
    client = _client("lamp-1", "wrong")

    def on_connect(_c: object, _u: object, _f: object, reason: Any, _p: object) -> None:
        outcome.append(reason.is_failure)
        connected.set()

    client.on_connect = on_connect
    client.connect(HOST, PORT)
    client.loop_start()
    try:
        assert connected.wait(5)
        assert outcome == [True]
    finally:
        client.loop_stop()


def test_a_device_cannot_speak_for_another_device() -> None:
    listener, received = _listen("device/+/telemetry")
    fan = _client("fan-1", "devpass")
    fan.connect(HOST, PORT)
    fan.loop_start()
    try:
        fan.publish("device/lamp-1/telemetry", "forged", qos=1).wait_for_publish(5)
        fan.publish("device/fan-1/telemetry", "own", qos=1).wait_for_publish(5)

        seen = _collect_until(received, "own", 3)

        # Same connection, QoS 1, sent first: if the ACL allowed it, "forged" would arrive
        # before "own".
        assert "own" in seen, "its own topic must get through"
        assert "forged" not in seen, "the ACL must block a device faking another device's topic"
    finally:
        fan.loop_stop()
        listener.loop_stop()


def test_a_dead_connection_is_announced_by_its_last_will() -> None:
    listener, received = _listen("device/plug-1/status")
    plug = _client(
        "plug-1", "devpass", topic="device/plug-1/status", payload="offline", qos=1, retain=True
    )
    plug.connect(HOST, PORT, keepalive=1)
    plug.loop_start()
    plug.publish("device/plug-1/status", "online", qos=1, retain=True).wait_for_publish(5)
    assert "online" in _collect_until(received, "online", 3)

    # Cut the socket with no DISCONNECT, like a device losing power. The broker only
    # knows after about 1.5 keepalive periods, then publishes the Last Will for the device.
    plug.loop_stop()
    sock = plug.socket()
    assert sock is not None
    sock.close()
    try:
        assert "offline" in _collect_until(received, "offline", 6)
    finally:
        listener.loop_stop()


def test_a_site_cannot_speak_for_another_site() -> None:
    listener, received = _listen("frigate/+/reviews")
    site = _client("frigate-demo", "frigate-demo")
    site.connect(HOST, PORT)
    site.loop_start()
    try:
        site.publish("frigate/other/reviews", "forged", qos=1).wait_for_publish(5)
        site.publish("frigate/demo/reviews", "own", qos=1).wait_for_publish(5)

        seen = _collect_until(received, "own", 3)

        assert "own" in seen, "vigisift must be able to read the site's alerts"
        assert "forged" not in seen, "the ACL must block a site faking another site's alerts"
    finally:
        site.loop_stop()
        listener.loop_stop()


def test_a_device_cannot_forge_camera_alerts() -> None:
    listener, received = _listen("frigate/+/reviews")
    lamp = _client("lamp-1", "devpass")
    lamp.connect(HOST, PORT)
    lamp.loop_start()
    site = _client("frigate-demo", "frigate-demo")
    site.connect(HOST, PORT)
    site.loop_start()
    try:
        lamp.publish("frigate/demo/reviews", "forged", qos=1).wait_for_publish(5)
        site.publish("frigate/demo/reviews", "real", qos=1).wait_for_publish(5)

        seen = _collect_until(received, "real", 3)

        assert "real" in seen
        assert "forged" not in seen, "devices must not write to Frigate's topics"
    finally:
        lamp.loop_stop()
        site.loop_stop()
        listener.loop_stop()
