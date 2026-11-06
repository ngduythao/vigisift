import asyncio
import json

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import text

from tests.fakes.ports import FakeMqttClient
from vigisift.config import Settings
from vigisift.features.telemetry.ingest import Telemetry
from vigisift.infra.db.database import create_engine


def test_an_unknown_state_is_refused() -> None:
    with pytest.raises(ValidationError):
        Telemetry.model_validate_json('{"state": "EXPLODED"}')


async def test_telemetry_updates_the_device_state(
    client: AsyncClient, mqtt: FakeMqttClient, settings: Settings
) -> None:
    mqtt.deliver("device/fan-1/telemetry", json.dumps({"state": "ON"}))
    await asyncio.sleep(0.3)

    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT state FROM devices WHERE id = 'fan-1'"))
            assert result.scalar_one() == "ON"
            rows = await connection.execute(
                text("SELECT count(*) FROM telemetry WHERE device_id = 'fan-1'")
            )
            assert rows.scalar_one() == 1
    finally:
        await engine.dispose()


async def test_telemetry_from_an_unknown_device_is_dropped(
    client: AsyncClient, mqtt: FakeMqttClient, settings: Settings
) -> None:
    mqtt.deliver("device/ghost-9/telemetry", json.dumps({"state": "ON"}))
    await asyncio.sleep(0.3)

    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT count(*) FROM telemetry"))
            assert rows.scalar_one() == 0, "an unknown device must not create a record"
    finally:
        await engine.dispose()
