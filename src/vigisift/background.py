"""Periodic jobs that run inside the API process.

Each feature lists its own jobs (`background_jobs(runtime)` in its `jobs.py`); the app
only starts and stops them. A job runs every `interval_sec`; one that returns True still
has work waiting and runs again at once. A failed run is logged and the loop carries on:
one bad pass (a flaky database, a site offline) must not stop the job for good.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    name: str
    interval_sec: float
    # True means "more is waiting": run again without sleeping.
    run: Callable[[], Awaitable[bool | None]]


async def every(job: Job) -> None:
    while True:
        try:
            if await job.run():
                continue
        except Exception:
            log.exception("background job %s failed", job.name)
        await asyncio.sleep(job.interval_sec)


def start(jobs: Iterable[Job]) -> list[asyncio.Task[None]]:
    return [asyncio.create_task(every(job), name=job.name) for job in jobs]


async def stop(tasks: Iterable[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
