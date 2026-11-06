"""Queries on the devices table. Every feature reading/writing devices goes through here."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.domain.device import DeviceState


async def list_devices(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        text("SELECT id, name, room, kind, state FROM devices ORDER BY room, name")
    )
    return [dict(row._mapping) for row in rows]


async def device_name(session: AsyncSession, device_id: str) -> str | None:
    row = await session.execute(text("SELECT name FROM devices WHERE id = :id"), {"id": device_id})
    name: str | None = row.scalar_one_or_none()
    return name


async def lock_state(session: AsyncSession, device_id: str) -> DeviceState | None:
    """Read the state and hold the row lock until the transaction ends.

    Two commands for one device at once must queue, or both read the old
    state and both pass the transition check.
    """
    row = await session.execute(
        text("SELECT state FROM devices WHERE id = :id FOR UPDATE"), {"id": device_id}
    )
    state = row.scalar_one_or_none()
    return None if state is None else DeviceState(state)


async def set_state(session: AsyncSession, device_id: str, state: DeviceState) -> bool:
    row = await session.execute(
        text("UPDATE devices SET state = :state, updated_at = now() WHERE id = :id RETURNING id"),
        {"state": state.value, "id": device_id},
    )
    return row.scalar_one_or_none() is not None
