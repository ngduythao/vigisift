import asyncio
import json

from httpx import AsyncClient
from sqlalchemy import text

from tests.fakes.ports import FakeMqttClient
from vigisift.config import Settings
from vigisift.infra.db.database import create_engine
from vigisift.runtime import Runtime


async def _scalar(settings: Settings, sql: str, **params: object) -> object:
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(sql), params)).scalar_one_or_none()
    finally:
        await engine.dispose()


async def _state(settings: Settings, device_id: str) -> object:
    return await _scalar(settings, "SELECT state FROM devices WHERE id = :id", id=device_id)


async def test_last_will_marks_a_device_offline(
    client: AsyncClient, mqtt: FakeMqttClient, settings: Settings
) -> None:
    mqtt.deliver("device/lamp-1/status", "offline")
    await asyncio.sleep(0.2)

    assert await _state(settings, "lamp-1") == "OFFLINE"


async def test_coming_online_only_leaves_offline(
    client: AsyncClient, mqtt: FakeMqttClient, settings: Settings
) -> None:
    mqtt.deliver("device/plug-1/status", "online")
    mqtt.deliver("device/fan-1/status", "online")
    await asyncio.sleep(0.2)

    assert await _state(settings, "plug-1") == "ONLINE", "OFFLINE → ONLINE"
    assert await _state(settings, "fan-1") == "OFF", "online must not overwrite OFF"


async def test_the_same_retained_status_twice_changes_nothing(
    runtime: Runtime, mqtt: FakeMqttClient, settings: Settings
) -> None:
    events: list[str] = []
    with runtime.hub.subscribe() as queue:
        mqtt.deliver("device/lamp-1/status", "offline")
        mqtt.deliver("device/lamp-1/status", "offline")
        await asyncio.sleep(0.3)
        while not queue.empty():
            events.append(queue.get_nowait())

    presence = [e for e in events if json.loads(e).get("type") == "presence"]
    assert len(presence) == 1, "retained is resent on every subscribe; report only real changes"


async def test_devices_report_whether_they_are_still_talking(
    client: AsyncClient, mqtt: FakeMqttClient
) -> None:
    mqtt.deliver("device/fan-1/telemetry", json.dumps({"state": "ON"}))
    await asyncio.sleep(0.3)

    devices = {d["id"]: d for d in (await client.get("/devices")).json()}

    assert devices["fan-1"]["online"] is True
    assert devices["lamp-1"]["online"] is False, "never sent a heartbeat"
