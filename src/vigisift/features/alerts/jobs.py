import logging

from vigisift.background import Job
from vigisift.features.alerts.reconcile import ReconcilePolicy, Reconciler
from vigisift.runtime import Runtime

log = logging.getLogger(__name__)


def reconciler(runtime: Runtime) -> Reconciler:
    settings = runtime.settings
    return Reconciler(
        runtime.session,
        runtime.frigate,
        runtime.events,
        runtime.cooldown,
        ReconcilePolicy(
            interval_sec=settings.frigate_reconcile_interval_sec,
            overlap_sec=settings.frigate_reconcile_overlap_sec,
            initial_lookback_sec=settings.frigate_initial_lookback_sec,
        ),
    )


def background_jobs(runtime: Runtime) -> list[Job]:
    recover = reconciler(runtime)
    first = True

    async def reconcile() -> None:
        nonlocal first
        # The first pass skips the lock: see Reconciler.run_once.
        force, first = first, False
        for result in await recover.run_once(force=force):
            if result.changed:
                log.info(
                    "reconcile %s: recovered %d/%d reviews",
                    result.site_id,
                    result.changed,
                    result.seen,
                )

    return [Job("reconcile", runtime.settings.frigate_reconcile_interval_sec, reconcile)]
