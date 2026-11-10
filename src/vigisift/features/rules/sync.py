"""Keep each site's Frigate running the rules VigiSift holds.

Pushes again rules that never reached Frigate (it was off when an admin saved), were
changed on Frigate's side, or were rebuilt from new built-in guidance. Runs on its own
schedule, apart from alert reconcile: the two change for different reasons, and a
rules problem must never hold back alerts.
"""

import logging

from sqlalchemy import text

from vigisift.features.rules import repository
from vigisift.features.rules.apply import detect_drift, push_pending
from vigisift.ports.cache import Cooldown
from vigisift.ports.frigate import FrigateApi, FrigateSite, FrigateUnavailableError
from vigisift.runtime import SessionScope

log = logging.getLogger(__name__)


class RuleSync:
    def __init__(
        self, session: SessionScope, frigate: FrigateApi, lock: Cooldown, interval_sec: float
    ) -> None:
        self._session = session
        self._frigate = frigate
        self._lock = lock
        self._interval_sec = interval_sec

    async def run_once(self, *, force: bool = False) -> int:
        """Sync every site once. Returns the number of cameras pushed.

        Locked per site like reconcile, so several instances don't push the same rules;
        `force` skips the lock (startup, and tests).
        """
        async with self._session() as db:
            rows = await db.execute(text("SELECT id, api_url FROM sites"))
            sites = [FrigateSite(row.id, row.api_url) for row in rows]
        pushed = 0
        ttl = max(1, int(self._interval_sec * 0.9))
        for site in sites:
            if not await self._lock.acquire(f"rules:{site.id}", ttl) and not force:
                continue
            try:
                pushed += await self._sync(site)
            except Exception:
                # One broken site must not block the others.
                log.exception("rule sync %s failed", site.id)
        return pushed

    async def _sync(self, site: FrigateSite) -> int:
        async with self._session() as db:
            for camera in await repository.rerender(db, site.id):
                log.info(
                    "camera rule %s rebuilt from the new system prompt, will push again", camera
                )
        # Separate errors: a config read error must not block pushing pending rules.
        try:
            await detect_drift(self._session, self._frigate, site)
        except FrigateUnavailableError as error:
            log.warning("can't read Frigate config to compare rules: %s", error)
        return await push_pending(self._session, self._frigate, site)
