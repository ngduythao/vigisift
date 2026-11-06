from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.domain.device import DeviceState
from vigisift.features.devices import repository


class Telemetry(BaseModel):
    """Only `state` is required; other readings (lux, temperature...) are kept as they are."""

    model_config = ConfigDict(extra="allow", frozen=True)

    state: DeviceState


async def store_telemetry(session: AsyncSession, device_id: str, telemetry: Telemetry) -> bool:
    """Record telemetry and update the device state.

    Unknown devices are **ignored, not created**: a mistyped topic would spawn a
    ghost device, and later nobody would know where it came from.
    """
    if not await repository.set_state(session, device_id, telemetry.state):
        return False
    await session.execute(
        text(
            "INSERT INTO telemetry (device_id, payload_json, received_at)"
            " VALUES (:id, CAST(:payload AS jsonb), now())"
        ),
        {"id": device_id, "payload": telemetry.model_dump_json()},
    )
    return True
