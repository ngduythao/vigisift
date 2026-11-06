import asyncio
import logging

from vigisift.features.rules import repository
from vigisift.ports.frigate import (
    FrigateApi,
    FrigateRejectedError,
    FrigateSite,
    FrigateUnavailableError,
)
from vigisift.runtime import SessionScope

log = logging.getLogger(__name__)

# Ceiling for one push, sign-in included: the rule row stays locked meanwhile, and
# an admin saving rules for this camera has to wait.
PUSH_TIMEOUT_SEC = 30


async def detect_drift(session: SessionScope, frigate: FrigateApi, site: FrigateSite) -> list[str]:
    """Compare applied rules with the RUNNING Frigate config. Returns cameras that drifted.

    If someone edits or restores Frigate's config.yml, VigiSift still thinks the rules are
    applied; re-reading the real config each reconcile pass is the only way to know.
    """
    async with session() as db:
        expected = await repository.applied(db, site.id)
    if not expected:
        return []
    running = await frigate.review_rules(site)
    drifted = [
        row
        for row in expected
        if running.get(row["camera"]) != (row["prompt"], list(row["concerns"]))
    ]
    for row in drifted:
        log.warning("camera rule %s/%s drifted from Frigate, pushing again", site.id, row["camera"])
        async with session() as db:
            await repository.forget_applied(db, site.id, row["camera"], row["sha"])
    return [row["camera"] for row in drifted]


async def push_pending(
    session: SessionScope, frigate: FrigateApi, site: FrigateSite, camera: str | None = None
) -> int:
    """Push a site's unapplied rules (or one camera's) to Frigate. Returns the number applied.

    Each camera pushes in a transaction holding the rule row's lock, so two simultaneous
    pushes can't put an old version over a new one. If Frigate is off, slow, or the account
    lacks permission, skip that camera and push next pass. If Frigate rejects the rules
    themselves (no such camera, bad config), record the reason for admins to see, and
    don't push that version again until an admin edits it.
    """
    async with session() as db:
        cameras = [camera] if camera else await repository.pending_cameras(db, site.id)
    applied = 0
    for name in cameras:
        async with session() as db:
            row = await repository.lock_pending(db, site.id, name)
            if row is None:
                continue
            error: str | None = None
            try:
                async with asyncio.timeout(PUSH_TIMEOUT_SEC):
                    await frigate.set_review_rules(
                        site, name, prompt=row["prompt"], concerns=list(row["concerns"])
                    )
                applied += 1
            except FrigateRejectedError as rejected:
                error = str(rejected)
                log.warning("camera rule %s/%s rejected by Frigate: %s", site.id, name, error)
            except (FrigateUnavailableError, TimeoutError) as unavailable:
                log.warning(
                    "camera rule %s/%s not pushed yet: %s",
                    site.id,
                    name,
                    str(unavailable) or "timed out",
                )
                continue
            await repository.mark(db, site.id, name, row["sha"], error)
    return applied
