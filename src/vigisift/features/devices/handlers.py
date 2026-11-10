from pydantic import ValidationError

from vigisift.domain.events import EventType
from vigisift.features import topics
from vigisift.features.devices.commands import Ack, CommandService
from vigisift.features.devices.presence import InvalidStatusError, apply_status, parse_status
from vigisift.runtime import Runtime


async def on_status(runtime: Runtime, topic: str, raw: str) -> None:
    try:
        device_id = topics.device_id(topic, "status")
        online = parse_status(raw)
    except (topics.InvalidTopicError, InvalidStatusError):
        return

    await (runtime.presence.touch(device_id) if online else runtime.presence.forget(device_id))
    async with runtime.session() as session:
        changed = await apply_status(session, device_id, online)
    if changed is not None:
        await runtime.events.publish(
            {"type": EventType.PRESENCE, "device_id": device_id, "state": changed}
        )


async def on_ack(runtime: Runtime, topic: str, raw: str) -> None:
    try:
        device_id = topics.device_id(topic, "ack")
        ack = Ack.model_validate_json(raw)
    except (topics.InvalidTopicError, ValidationError):
        return

    async with runtime.session() as session:
        resolved = await CommandService(session, runtime.mqtt).resolve(device_id, ack)
    if resolved is not None:
        await runtime.events.publish(resolved.event())


async def expire_commands(runtime: Runtime) -> int:
    async with runtime.session() as session:
        expired = await CommandService(session, runtime.mqtt).expire(
            runtime.settings.command_timeout_sec
        )
    for command in expired:
        await runtime.events.publish(command.event())
    return len(expired)
