from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.domain.device import DeviceState, validate_transition
from vigisift.features.devices import repository
from vigisift.ports.cache import Presence


class InvalidStatusError(Exception):
    pass


def parse_status(raw: str) -> bool:
    """`online` or `offline`, retained on `device/<id>/status`.

    A device publishes `online` itself on connecting; the BROKER publishes `offline` for
    it through the Last Will when a connection dies without notice (power cut, wifi drop).
    Retained, so a restarted server knows at once who is alive.
    """
    value = raw.strip().lower()
    if value not in {"online", "offline"}:
        raise InvalidStatusError(f"invalid status: {raw!r}")
    return value == "online"


async def apply_status(session: AsyncSession, device_id: str, online: bool) -> DeviceState | None:
    """Change state by presence; returns the new state, or None if unchanged.

    `online` only lifts a device out of OFFLINE; a device that's ON stays ON.
    Retained messages are resent on every subscribe, so this function must tolerate
    receiving the same status many times.
    """
    current = await repository.lock_state(session, device_id)
    if current is None or online == (current != DeviceState.OFFLINE):
        return None
    target = DeviceState.ONLINE if online else DeviceState.OFFLINE
    validate_transition(current, target)
    await repository.set_state(session, device_id, target)
    return target


async def list_with_liveness(session: AsyncSession, presence: Presence) -> list[dict[str, Any]]:
    """State from Postgres plus heartbeats from Valkey.

    `state` is what the device last reported; `online` is whether it's still talking.
    Two different things: a light can be ON yet silent for 5 minutes.
    """
    devices = await repository.list_devices(session)
    online = await presence.online()
    return [{**d, "online": d["id"] in online} for d in devices]
