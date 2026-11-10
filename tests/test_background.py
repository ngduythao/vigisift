import asyncio
import logging

import pytest

from vigisift import background
from vigisift.background import Job


async def test_a_job_with_work_waiting_runs_again_without_sleeping() -> None:
    runs = 0

    async def drain() -> bool:
        nonlocal runs
        runs += 1
        return runs < 3

    [task] = background.start([Job("drain", 3600, drain)])
    await asyncio.sleep(0.05)
    await background.stop([task])

    assert runs == 3, "two full passes, then one that empties the queue, then a long sleep"


async def test_a_failed_run_is_logged_and_the_job_keeps_going(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runs = 0

    async def flaky() -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("database went away")

    with caplog.at_level(logging.ERROR):
        [task] = background.start([Job("flaky", 0.01, flaky)])
        await asyncio.sleep(0.1)
        await background.stop([task])

    assert runs > 1
    assert "background job flaky failed" in caplog.text


async def test_stop_cancels_every_task() -> None:
    async def idle() -> None:
        return None

    tasks = background.start([Job("a", 3600, idle), Job("b", 3600, idle)])
    await background.stop(tasks)

    assert all(task.cancelled() for task in tasks)


async def test_a_job_can_wait_before_its_first_run() -> None:
    runs = 0

    async def count() -> None:
        nonlocal runs
        runs += 1

    [task] = background.start([Job("later", 3600, count, first_run_after_sec=3600)])
    await asyncio.sleep(0.05)
    await background.stop([task])

    assert runs == 0
