import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.fakes.ports import FakeMqttClient
from vigisift.config import Settings
from vigisift.features.devices.handlers import expire_commands
from vigisift.infra.db.database import create_engine
from vigisift.runtime import Runtime


@pytest.fixture
async def command_id(client: AsyncClient) -> AsyncIterator[str]:
    response = await client.post(
        "/devices/lamp-1/commands", json={"action": "ON"}, headers={"Idempotency-Key": "k-ack"}
    )
    yield response.json()["command_id"]


async def test_a_command_reaches_the_device_over_mqtt(
    client: AsyncClient, mqtt: FakeMqttClient
) -> None:
    response = await client.post(
        "/devices/lamp-1/commands", json={"action": "ON"}, headers={"Idempotency-Key": "k1"}
    )

    assert response.status_code == 200
    assert response.json()["duplicate"] is False
    topics = [topic for topic, _ in mqtt.published]
    assert topics == ["device/lamp-1/command"]


async def test_the_same_key_twice_sends_one_command(
    client: AsyncClient, mqtt: FakeMqttClient
) -> None:
    headers = {"Idempotency-Key": "double-tap"}
    first = await client.post("/devices/lamp-1/commands", json={"action": "ON"}, headers=headers)
    second = await client.post("/devices/lamp-1/commands", json={"action": "ON"}, headers=headers)

    assert second.json()["command_id"] == first.json()["command_id"]
    assert second.json()["duplicate"] is True
    assert len(mqtt.published) == 1, "a double click must not send the device two commands"


async def test_an_offline_device_refuses_to_turn_on(client: AsyncClient) -> None:
    response = await client.post(
        "/devices/plug-1/commands", json={"action": "ON"}, headers={"Idempotency-Key": "k2"}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "cannot_go_from_offline_to_on"


async def test_an_unknown_device_is_not_found(client: AsyncClient) -> None:
    response = await client.post(
        "/devices/ghost/commands", json={"action": "ON"}, headers={"Idempotency-Key": "k3"}
    )

    assert response.status_code == 404


async def test_a_command_goes_out_with_qos_1(
    client: AsyncClient, mqtt: FakeMqttClient, command_id: str
) -> None:
    assert mqtt.options[-1] == (1, False)
    assert (await client.get(f"/commands/{command_id}")).json()["status"] == "PENDING"


async def test_an_ack_settles_the_command(
    client: AsyncClient, mqtt: FakeMqttClient, command_id: str
) -> None:
    mqtt.deliver("device/lamp-1/ack", json.dumps({"command_id": command_id, "ok": True}))
    await asyncio.sleep(0.2)

    assert (await client.get(f"/commands/{command_id}")).json()["status"] == "ACKED"


async def test_a_failed_ack_is_recorded(
    client: AsyncClient, mqtt: FakeMqttClient, command_id: str
) -> None:
    mqtt.deliver("device/lamp-1/ack", json.dumps({"command_id": command_id, "ok": False}))
    await asyncio.sleep(0.2)

    assert (await client.get(f"/commands/{command_id}")).json()["status"] == "FAILED"


async def test_one_device_cannot_ack_for_another(
    client: AsyncClient, mqtt: FakeMqttClient, command_id: str
) -> None:
    mqtt.deliver("device/fan-1/ack", json.dumps({"command_id": command_id, "ok": True}))
    await asyncio.sleep(0.2)

    assert (await client.get(f"/commands/{command_id}")).json()["status"] == "PENDING"


async def test_a_late_ack_cannot_revive_a_timed_out_command(
    client: AsyncClient,
    runtime: Runtime,
    mqtt: FakeMqttClient,
    command_id: str,
    settings: Settings,
) -> None:
    engine = create_engine(settings)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE commands SET created_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": command_id},
        )
    await engine.dispose()

    expired = await expire_commands(runtime)
    mqtt.deliver("device/lamp-1/ack", json.dumps({"command_id": command_id, "ok": True}))
    await asyncio.sleep(0.2)

    assert expired == 1
    assert (await client.get(f"/commands/{command_id}")).json()["status"] == "TIMED_OUT"


async def test_an_unknown_command_is_404(client: AsyncClient) -> None:
    response = await client.get("/commands/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
