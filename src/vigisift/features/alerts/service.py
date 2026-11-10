"""The one way to change an alert: write it, commit, then tell the console.

Every writer of `camera_alerts` (MQTT, reconcile, the rule re-check) goes through
`alert_writer`, so a new writer can't forget to publish: the screen would then stay stale
with nothing reporting it. Events go out only after the transaction commits, so the
console never reloads an alert that a rollback took back.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.domain.alert import AlertUpdate
from vigisift.domain.events import EventType
from vigisift.features.alerts import repository
from vigisift.ports.cache import EventBus
from vigisift.runtime import SessionScope


def alert_event(alert: dict[str, Any]) -> dict[str, Any]:
    """An alert as a JSON event for the WebSocket (the event bus only takes JSON types)."""
    return {
        "type": EventType.CAMERA_ALERT,
        **{
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in alert.items()
        },
    }


class AlertWriter:
    """Alert writes inside one transaction; `changed` collects what to publish after commit."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.changed: list[dict[str, Any]] = []

    async def record(self, update: AlertUpdate) -> bool:
        """Merge a report from Frigate. True if the alert changed."""
        alert = await repository.record(self.db, update)
        if alert is not None:
            self.changed.append(alert)
        return alert is not None

    async def save_check(
        self, site_id: str, review_id: str, *, level: int, match: str | None, sha: str
    ) -> None:
        self.changed.append(
            await repository.save_check(
                self.db, site_id, review_id, level=level, match=match, sha=sha
            )
        )


@asynccontextmanager
async def alert_writer(session: SessionScope, events: EventBus) -> AsyncIterator[AlertWriter]:
    async with session() as db:
        writer = AlertWriter(db)
        yield writer
    for alert in writer.changed:
        await events.publish(alert_event(alert))
