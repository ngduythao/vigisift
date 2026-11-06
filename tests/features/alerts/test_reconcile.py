import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from tests.fakes.ports import FakeFrigateApi
from tests.features.alerts.frigate import START, SUMMARY, segment
from vigisift.features.alerts import reconcile
from vigisift.features.alerts.reconcile import ReconcilePolicy, Reconciler
from vigisift.runtime import Runtime

POLICY = ReconcilePolicy(interval_sec=300, overlap_sec=900, initial_lookback_sec=86400)


class AlwaysLock:
    async def acquire(self, key: str, seconds: int) -> bool:
        return True


async def _settled(runtime: Runtime) -> None:
    """Wait for the app's startup reconcile pass (with an empty fake Frigate) to finish,
    then clear its mark for the test to set."""
    for _ in range(40):
        async with runtime.session() as session:
            done = (
                await session.execute(text("SELECT synced_until FROM sites WHERE id = 'demo'"))
            ).scalar_one()
        if done is not None:
            break
        await asyncio.sleep(0.05)
    async with runtime.session() as session:
        await session.execute(text("UPDATE sites SET synced_until = NULL"))


def _reconciler(runtime: Runtime, frigate: FakeFrigateApi, lock: Any = None) -> Reconciler:
    return Reconciler(runtime.session, frigate, runtime.events, lock or AlwaysLock(), POLICY)


async def _review_ids(runtime: Runtime) -> set[str]:
    async with runtime.session() as session:
        rows = await session.execute(text("SELECT review_id FROM camera_alerts"))
        return {row.review_id for row in rows}


async def _synced_until(runtime: Runtime, site: str) -> datetime | None:
    async with runtime.session() as session:
        value: datetime | None = (
            await session.execute(
                text("SELECT synced_until FROM sites WHERE id = :id"), {"id": site}
            )
        ).scalar_one()
        return value


def _ended(review_id: str, start: float = START, **kwargs: Any) -> dict[str, Any]:
    return segment(review_id, severity="alert", start=start, end=start + 10, **kwargs)


async def test_reviews_missed_on_mqtt_are_recovered(runtime: Runtime) -> None:
    await _settled(runtime)
    frigate = FakeFrigateApi({"demo": [_ended("1.0-a"), _ended("2.0-b", metadata=SUMMARY)]})

    with runtime.hub.subscribe() as browser:
        [result] = await _reconciler(runtime, frigate).run_once()
        await asyncio.sleep(0.3)
        pushed = browser.qsize()

    assert (result.seen, result.changed) == (2, 2)
    assert await _review_ids(runtime) == {"1.0-a", "2.0-b"}
    assert pushed == 2, "recovered alerts must also reach the operator's screen"
    synced = await _synced_until(runtime, "demo")
    assert synced is not None and abs(synced.timestamp() - time.time()) < 5


async def test_reading_the_same_reviews_again_changes_nothing(runtime: Runtime) -> None:
    await _settled(runtime)
    # Ended a minute ago: still inside the second read's overlap.
    frigate = FakeFrigateApi({"demo": [_ended("1.0-a", start=time.time() - 70)]})
    reconciler = _reconciler(runtime, frigate)

    await reconciler.run_once()
    [again] = await reconciler.run_once()

    assert (again.seen, again.changed) == (1, 0)


async def test_the_window_starts_before_the_last_sync_by_the_overlap(runtime: Runtime) -> None:
    await _settled(runtime)
    last = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
    async with runtime.session() as session:
        await session.execute(text("UPDATE sites SET synced_until = :t"), {"t": last})
    frigate = FakeFrigateApi()

    await _reconciler(runtime, frigate).run_once()

    [(_, after, _, _)] = frigate.calls
    assert after == last.timestamp() - POLICY.overlap_sec


async def test_a_site_never_synced_looks_back_a_day(runtime: Runtime) -> None:
    await _settled(runtime)
    frigate = FakeFrigateApi()

    await _reconciler(runtime, frigate).run_once()

    [(_, after, before, _)] = frigate.calls
    assert before - after == pytest.approx(POLICY.initial_lookback_sec, abs=1)


async def test_long_gaps_are_read_page_by_page(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _settled(runtime)
    monkeypatch.setattr(reconcile, "PAGE_SIZE", 2)
    reviews = [_ended(f"{i}.0-p", start=START - i * 60) for i in range(5)]
    frigate = FakeFrigateApi({"demo": reviews})

    [result] = await _reconciler(runtime, frigate).run_once()

    assert result.seen == 5
    assert await _review_ids(runtime) == {f"{i}.0-p" for i in range(5)}
    befores = [before for _, _, before, _ in frigate.calls]
    assert befores == sorted(befores, reverse=True), "each page moves back in time"


async def test_one_broken_site_does_not_stop_the_others(runtime: Runtime) -> None:
    await _settled(runtime)
    async with runtime.session() as session:
        await session.execute(
            text(
                "INSERT INTO sites (id, name, ui_url, api_url)"
                " VALUES ('kho', 'Kho', 'http://kho.test', 'http://kho.test:5000')"
            )
        )
    frigate = FakeFrigateApi({"kho": [_ended("1.0-k", camera="gate")]})
    frigate.failing.add("demo")

    results = await _reconciler(runtime, frigate).run_once()

    assert [r.site_id for r in results] == ["kho"]
    assert await _review_ids(runtime) == {"1.0-k"}
    assert await _synced_until(runtime, "demo") is None, (
        "a failed site keeps its old mark to re-read next cycle"
    )


async def test_a_malformed_review_is_skipped_and_the_rest_recorded(runtime: Runtime) -> None:
    await _settled(runtime)
    bad = _ended("2.0-bad") | {"severity": "panic"}
    frigate = FakeFrigateApi({"demo": [_ended("1.0-ok"), bad]})

    [result] = await _reconciler(runtime, frigate).run_once()

    assert await _review_ids(runtime) == {"1.0-ok"}
    assert result.seen == 2
    assert await _synced_until(runtime, "demo") is not None


async def test_two_instances_do_not_reconcile_the_same_site_at_once(runtime: Runtime) -> None:
    await _settled(runtime)
    async with runtime.session() as session:
        await session.execute(
            text(
                "INSERT INTO sites (id, name, ui_url, api_url)"
                " VALUES ('fresh', 'New', 'http://fresh.test', 'http://fresh.test:5000')"
            )
        )
    frigate = FakeFrigateApi()
    a = _reconciler(runtime, frigate, runtime.cooldown)
    b = _reconciler(runtime, frigate, runtime.cooldown)

    first, second = await a.run_once(), await b.run_once()

    assert "fresh" in [r.site_id for r in first]
    assert "fresh" not in [r.site_id for r in second], "the other instance did it this cycle"


async def test_the_startup_pass_ignores_a_leftover_lock(runtime: Runtime) -> None:
    """Just after a restart messages were most likely missed: don't wait for an old lock."""
    await _settled(runtime)

    class HeldLock:
        async def acquire(self, key: str, seconds: int) -> bool:
            return False

    frigate = FakeFrigateApi({"demo": [_ended("1.0-gap")]})
    reconciler = _reconciler(runtime, frigate, HeldLock())

    assert await reconciler.run_once() == [], "a normal pass honours the lock"
    [result] = await reconciler.run_once(force=True)
    assert result.changed == 1


async def test_a_failed_rules_check_does_not_hold_back_the_alert_cursor(runtime: Runtime) -> None:
    await _settled(runtime)
    frigate = FakeFrigateApi({"demo": [_ended("1.0-a")]})
    frigate.config_failing.add("demo")
    async with runtime.session() as session:
        await session.execute(
            text(
                "INSERT INTO camera_rules (site_id, camera, rules, prompt, sha, applied_sha)"
                " VALUES ('demo', 'shop_aisle', '{}', 'p', 'x', 'x')"
            )
        )

    await _reconciler(runtime, frigate).run_once()

    assert await _review_ids(runtime) == {"1.0-a"}
    assert await _synced_until(runtime, "demo") is not None
