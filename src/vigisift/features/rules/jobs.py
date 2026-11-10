from vigisift.background import Job
from vigisift.features.rules.check import BATCH, check_pending
from vigisift.features.rules.sync import RuleSync
from vigisift.runtime import Runtime


def rule_sync(runtime: Runtime) -> RuleSync:
    return RuleSync(
        runtime.session,
        runtime.frigate,
        runtime.cooldown,
        runtime.settings.rule_sync_interval_sec,
    )


def background_jobs(runtime: Runtime) -> list[Job]:
    settings = runtime.settings
    sync = rule_sync(runtime)
    first = True

    async def push() -> None:
        nonlocal first
        force, first = first, False
        await sync.run_once(force=force)

    jobs = [Job("rule-sync", settings.rule_sync_interval_sec, push)]
    picker = runtime.rule_picker
    if picker is not None:

        async def check() -> bool:
            # A full batch means more are waiting.
            return await check_pending(runtime.session, picker, runtime.events) == BATCH

        jobs.append(Job("rule-check", settings.rule_check_interval_sec, check))
    return jobs
