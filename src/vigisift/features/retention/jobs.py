import logging

from vigisift.background import Job
from vigisift.features.retention.job import Policy, purge
from vigisift.runtime import Runtime

log = logging.getLogger(__name__)

# Woken hourly, it runs once a day: the lock outlives 23 hours of wake-ups, and also
# stops several instances running it together. Nothing is urgent at startup, so the first
# wake-up is an hour in (a crash loop doesn't start a purge on each restart).
WAKE_SEC = 3600
ONCE_A_DAY_SEC = 23 * 3600


def background_jobs(runtime: Runtime) -> list[Job]:
    settings = runtime.settings
    policy = Policy(settings.retention_days, settings.reviewed_retention_days)

    async def run() -> None:
        if not await runtime.cooldown.acquire("retention", ONCE_A_DAY_SEC):
            return
        deleted = await purge(runtime.session, policy)
        if any(deleted.values()):
            log.info("retention deleted %s", deleted)

    return [Job("retention", WAKE_SEC, run, first_run_after_sec=WAKE_SEC)]
