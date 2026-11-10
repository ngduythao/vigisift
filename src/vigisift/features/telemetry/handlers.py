from pydantic import ValidationError

from vigisift.domain.events import EventType
from vigisift.features import topics
from vigisift.features.telemetry.ingest import Telemetry, store_telemetry
from vigisift.runtime import Runtime


async def on_telemetry(runtime: Runtime, topic: str, raw: str) -> None:
    try:
        device_id = topics.device_id(topic, "telemetry")
        telemetry = Telemetry.model_validate_json(raw)
    except (topics.InvalidTopicError, ValidationError):
        return

    async with runtime.session() as session:
        stored = await store_telemetry(session, device_id, telemetry)
    if stored:
        # Each telemetry message is also a heartbeat.
        await runtime.presence.touch(device_id)
        await runtime.events.publish(
            # `type` last: devices may send extra fields, and must not pose as another event.
            {
                **telemetry.model_dump(mode="json"),
                "device_id": device_id,
                "type": EventType.TELEMETRY,
            }
        )
