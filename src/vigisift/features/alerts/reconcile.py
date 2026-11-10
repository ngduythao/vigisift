"""Recover reviews MQTT missed by asking each site's Frigate API directly.

MQTT is the fast path but makes no promise: a VigiSift restart, a broker drop,
or a flaky link between site and center all lose messages. Each cycle, each
site is reconciled from its mark minus an overlap, through the same merge function
MQTT uses, so re-reading what's already there changes nothing.
"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from vigisift.domain.alert import AlertUpdate
from vigisift.features.alerts.payload import InvalidReviewError, parse_review_segment
from vigisift.features.alerts.service import alert_writer
from vigisift.ports.cache import Cooldown, EventBus
from vigisift.ports.frigate import FrigateApi, FrigateSite, FrigateUnavailableError
from vigisift.runtime import SessionScope

log = logging.getLogger(__name__)

PAGE_SIZE = 200


@dataclass(frozen=True)
class ReconcilePolicy:
    interval_sec: float
    overlap_sec: float
    initial_lookback_sec: float


@dataclass(frozen=True)
class SiteResult:
    site_id: str
    seen: int
    changed: int


class Reconciler:
    def __init__(
        self,
        session: SessionScope,
        frigate: FrigateApi,
        events: EventBus,
        lock: Cooldown,
        policy: ReconcilePolicy,
    ) -> None:
        self._session = session
        self._frigate = frigate
        self._events = events
        self._lock = lock
        self._policy = policy

    async def run_once(self, *, force: bool = False) -> list[SiteResult]:
        """Reconcile every site once.

        `force` skips the lock: for the startup pass, because just after a restart
        is when messages were most likely missed, and the previous pass's lock (this
        instance's own, before stopping) may still exist. A duplicate run is harmless
        because repeated merges don't change the result.
        """
        async with self._session() as session:
            rows = await session.execute(text("SELECT id, api_url, synced_until FROM sites"))
            sites = [(FrigateSite(row.id, row.api_url), row.synced_until) for row in rows]
        results = []
        for site, synced_until in sites:
            # Several instances run this loop: lock per site so only one instance
            # reconciles each cycle. The lock expires before the next cycle (90%) so the next
            # cycle surely gets it; if an instance dies midway the lock expires by itself.
            ttl = max(1, int(self._policy.interval_sec * 0.9))
            if not await self._lock.acquire(f"reconcile:{site.id}", ttl) and not force:
                continue
            try:
                results.append(await self._reconcile(site, synced_until))
            except FrigateUnavailableError as error:
                # The error already carries the site id.
                log.warning("reconcile can't read Frigate: %s", error)
            except Exception:
                # One broken site (flaky DB, odd data) must not block the others.
                log.exception("reconcile %s failed", site.id)
        return results

    async def _reconcile(self, site: FrigateSite, synced_until: datetime | None) -> SiteResult:
        now = time.time()
        cursor = (
            synced_until.timestamp() - self._policy.overlap_sec
            if synced_until is not None
            else now - self._policy.initial_lookback_sec
        )
        seen = changed = 0
        before = now
        while True:
            page = await self._frigate.reviews(site, after=cursor, before=before, limit=PAGE_SIZE)
            updates = self._parse(site.id, page)
            changed += await self._apply(updates)
            seen += len(page)
            if len(page) < PAGE_SIZE:
                break
            # Next page: reviews starting before this page's oldest review
            # (Frigate sorts latest start first, filtering `start_time < before`).
            oldest = min((u.started_at.timestamp() for u in updates), default=before)
            if oldest >= before:
                log.warning("reconcile %s: can't paginate further, stopping at %s", site.id, before)
                break
            before = oldest
        # Move the mark only after reading everything: after an error, retry from the old mark.
        async with self._session() as session:
            await session.execute(
                text("UPDATE sites SET synced_until = :until WHERE id = :id"),
                {"until": datetime.fromtimestamp(now, UTC), "id": site.id},
            )
        return SiteResult(site.id, seen, changed)

    @staticmethod
    def _parse(site_id: str, page: list[dict[str, Any]]) -> list[AlertUpdate]:
        updates = []
        for raw in page:
            try:
                updates.append(parse_review_segment(site_id, raw))
            except InvalidReviewError as error:
                log.warning("reconcile %s: dropped a review, %s", site_id, error)
        return updates

    async def _apply(self, updates: list[AlertUpdate]) -> int:
        changed = 0
        for update in updates:
            # One transaction per review: one bad row doesn't take the whole page with it.
            async with alert_writer(self._session, self._events) as writer:
                changed += await writer.record(update)
        return changed
