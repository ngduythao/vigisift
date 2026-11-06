import asyncio
import logging

from pydantic import ValidationError

from vigisift.features import topics
from vigisift.features.devices.commands import Ack, CommandService
from vigisift.features.devices.presence import InvalidStatusError, apply_status, parse_status
from vigisift.runtime import Runtime

log = logging.getLogger(__name__)


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
        await runtime.events.publish({"type": "presence", "device_id": device_id, "state": changed})


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


async def sweep_forever(runtime: Runtime) -> None:
    while True:
        try:
            await expire_commands(runtime)
        except Exception:
            # A failed sweep (flaky DB) must not kill the loop.
            log.exception("overdue command sweep failed")
        await asyncio.sleep(runtime.settings.command_sweep_sec)
