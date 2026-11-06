import logging
from datetime import datetime
from typing import Any

from vigisift.features import topics
from vigisift.features.alerts import repository
from vigisift.features.alerts.payload import InvalidReviewError, parse_review_message
from vigisift.runtime import Runtime

log = logging.getLogger(__name__)


def alert_event(alert: dict[str, Any]) -> dict[str, Any]:
    """An alert as a JSON event for the WebSocket (the event bus only takes JSON types)."""
    return {
        "type": "camera_alert",
        **{
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in alert.items()
        },
    }


async def on_review(runtime: Runtime, topic: str, raw: str) -> None:
    try:
        site_id = topics.frigate_site(topic, "reviews")
        update = parse_review_message(site_id, raw)
    except (topics.InvalidTopicError, InvalidReviewError) as error:
        log.warning("dropped review on %s: %s", topic, error)
        return

    try:
        async with runtime.session() as session:
            alert = await repository.record(session, update)
    except repository.UnknownSiteError:
        log.warning("dropped review %s: site %s is not registered", update.review_id, site_id)
        return
    if alert is not None:
        await runtime.events.publish(alert_event(alert))
