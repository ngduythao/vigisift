"""The broker's TLS listener (8883), with the dev CA from `make certs`."""

import queue
import socket
import ssl
import time
import uuid

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from tests.conftest import ROOT
from vigisift.infra.mqtt.client import PahoMqttClient

HOST, TLS_PORT = "127.0.0.1", 8883
CA = ROOT / "deploy" / "certs" / "ca.crt"


def _listening() -> bool:
    try:
        with socket.create_connection((HOST, TLS_PORT), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not CA.exists() or not _listening(),
    reason="no TLS listener: run `make certs`, then restart mosquitto",
)


def _client() -> paho.Client:
    client = paho.Client(CallbackAPIVersion.VERSION2, client_id=f"test-{uuid.uuid4().hex[:8]}")
    client.username_pw_set("vigisift", "vigisift")
    return client


def test_vigisift_talks_to_the_broker_over_tls() -> None:
    received: queue.Queue[str] = queue.Queue()
    listener = _client()
    listener.tls_set(ca_certs=str(CA))
    listener.on_message = lambda _c, _u, m: received.put(m.payload.decode())
    listener.connect(HOST, TLS_PORT)
    listener.loop_start()
    subscribed = listener.subscribe("frigate/+/reviews", qos=1)
    assert subscribed[0] == paho.MQTT_ERR_SUCCESS

    site = PahoMqttClient(HOST, TLS_PORT, "frigate-demo", "frigate-demo", tls=True, ca_file=str(CA))
    try:
        deadline = time.monotonic() + 5
        seen: list[str] = []
        while time.monotonic() < deadline and "over tls" not in seen:
            site.publish("frigate/demo/reviews", "over tls", qos=1)
            try:
                seen.append(received.get(timeout=0.5))
            except queue.Empty:
                continue
        assert "over tls" in seen
    finally:
        site.close()
        listener.loop_stop()


def test_a_broker_certificate_from_an_unknown_ca_is_refused() -> None:
    client = _client()
    client.tls_set()  # the system's CAs only, which never signed the dev certificate

    with pytest.raises(ssl.SSLCertVerificationError):
        client.connect(HOST, TLS_PORT)
