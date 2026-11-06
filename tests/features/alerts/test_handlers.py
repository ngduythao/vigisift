import asyncio
import json
import logging
from typing import Any

import pytest
from httpx import AsyncClient

from tests.fakes.ports import FakeMqttClient
from tests.features.alerts.frigate import lifecycle, message, segment
from vigisift.runtime import Runtime


async def _events(
    runtime: Runtime, deliver: list[tuple[str, str]], mqtt: FakeMqttClient
) -> list[dict[str, Any]]:
    with runtime.hub.subscribe() as queue:
        for topic, raw in deliver:
            mqtt.deliver(topic, raw)
        await asyncio.sleep(0.5)
        seen: list[dict[str, Any]] = []
        while not queue.empty():
            seen.append(json.loads(queue.get_nowait()))
    return [event for event in seen if event.get("type") == "camera_alert"]


async def test_a_frigate_review_reaches_the_browser(
    client: AsyncClient, runtime: Runtime, mqtt: FakeMqttClient
) -> None:
    events = await _events(runtime, [("frigate/demo/reviews", lifecycle("1.0-live")["new"])], mqtt)

    [event] = events
    assert (event["site_id"], event["review_id"], event["severity"]) == (
        "demo",
        "1.0-live",
        "detection",
    )
    assert event["link"] == "http://frigate.test:8971/review?id=1.0-live"


async def test_a_duplicate_delivery_is_pushed_once(
    client: AsyncClient, runtime: Runtime, mqtt: FakeMqttClient
) -> None:
    raw = lifecycle("1.0-dup")["new"]

    events = await _events(runtime, [("frigate/demo/reviews", raw)] * 3, mqtt)

    assert len(events) == 1, "with a QoS 1 resend the operator still sees it once"


async def test_an_unregistered_site_is_logged_and_dropped(
    client: AsyncClient,
    runtime: Runtime,
    mqtt: FakeMqttClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        events = await _events(
            runtime, [("frigate/ghost/reviews", lifecycle("1.0-ghost")["new"])], mqtt
        )

    assert events == []
    assert "not registered" in caplog.text


async def test_a_malformed_review_is_logged_and_dropped(
    client: AsyncClient,
    runtime: Runtime,
    mqtt: FakeMqttClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = message("new", segment() | {"severity": "panic"})

    with caplog.at_level(logging.WARNING):
        events = await _events(runtime, [("frigate/demo/reviews", bad)], mqtt)

    assert events == []
    assert "severity" in caplog.text
    assert "panic" not in caplog.text, "payload content is not copied to the log"
