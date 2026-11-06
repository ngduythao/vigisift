import asyncio
import itertools
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.features.alerts.frigate import START, SUMMARY, lifecycle, message, segment
from vigisift.features.alerts import repository
from vigisift.features.alerts.payload import parse_review_message
from vigisift.runtime import Runtime

_EXPECTED = {
    "camera": "shop_aisle",
    "severity": "alert",
    "objects": ["person"],
    "zones": ["aisle"],
    "started_at": datetime.fromtimestamp(START, UTC),
    "ended_at": datetime.fromtimestamp(START + 12, UTC),
    "times_approximate": False,
    "threat_level": 1,
    "title": SUMMARY["title"],
    "summary": SUMMARY["shortSummary"],
}


async def _record(runtime: Runtime, raw: str, site: str = "demo") -> dict[str, Any] | None:
    async with runtime.session() as session:
        return await repository.record(session, parse_review_message(site, raw))


async def _row(runtime: Runtime, review_id: str) -> dict[str, Any]:
    async with runtime.session() as session:
        rows = await repository.recent(
            session, site_id="demo", since_minutes=10**6, min_threat_level=None, limit=200
        )
    [row] = [r for r in rows if r["review_id"] == review_id]
    return row


def _subset(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in _EXPECTED}


async def test_every_arrival_order_ends_in_the_same_alert(runtime: Runtime) -> None:
    """Shared subscriptions and reconcile reorder messages; the result must not change."""
    orders = list(itertools.permutations(["new", "update", "end", "genai"]))
    assert len(orders) == 24
    for index, order in enumerate(orders):
        review_id = f"{index}.0-perm"
        messages = lifecycle(review_id)
        for kind in order:
            await _record(runtime, messages[kind])

        assert _subset(await _row(runtime, review_id)) == _EXPECTED, f"order {order}"


async def test_replaying_everything_changes_nothing(runtime: Runtime) -> None:
    messages = lifecycle("1.0-replay")
    first = [await _record(runtime, raw) for raw in messages.values()]
    second = [await _record(runtime, raw) for raw in messages.values()]

    assert all(result is not None for result in first), "each first report changes the row"
    assert second == [None] * 4, "an identical repeat has nothing new to push to screens"


async def test_with_only_the_summary_the_times_are_marked_approximate(runtime: Runtime) -> None:
    """Missed all of new/end (VigiSift off), only the summary left: keep it, marked approximate."""
    await _record(runtime, lifecycle("1.0-late")["genai"])

    row = await _row(runtime, "1.0-late")
    assert row["times_approximate"] is True
    assert row["started_at"] == datetime.fromtimestamp(START - 5, UTC)
    assert row["threat_level"] == 1


async def test_a_new_summary_replaces_the_old_one_as_a_whole(runtime: Runtime) -> None:
    """Never mix one report's title with another's scene."""
    review_id = "1.0-regen"
    await _record(runtime, lifecycle(review_id)["genai"])
    second = SUMMARY | {"title": "Browsing goods", "potential_threat_level": 0, "scene": "y" * 200}
    await _record(runtime, message("genai", segment(review_id, metadata=second)))

    row = await _row(runtime, review_id)
    assert (row["title"], row["threat_level"], row["scene"]) == ("Browsing goods", 0, "y" * 200)


async def test_a_message_without_a_summary_never_erases_one(runtime: Runtime) -> None:
    review_id = "1.0-keep"
    await _record(runtime, lifecycle(review_id)["genai"])
    await _record(runtime, lifecycle(review_id)["end"])

    assert (await _row(runtime, review_id))["title"] == SUMMARY["title"]


async def test_two_instances_merging_the_same_review_at_once(runtime: Runtime) -> None:
    messages = lifecycle("1.0-race")

    await asyncio.gather(
        _record(runtime, messages["end"]),
        _record(runtime, messages["genai"]),
        _record(runtime, messages["new"]),
    )

    row = await _row(runtime, "1.0-race")
    assert (row["severity"], row["threat_level"], row["times_approximate"]) == ("alert", 1, False)


async def test_an_unregistered_site_is_refused(runtime: Runtime) -> None:
    with pytest.raises(repository.UnknownSiteError):
        await _record(runtime, lifecycle("1.0-ghost")["new"], site="ghost")


async def test_the_link_opens_the_review_in_that_sites_frigate(runtime: Runtime) -> None:
    alert = await _record(runtime, lifecycle("1.0-link")["new"])

    assert alert is not None
    assert alert["link"] == "http://frigate.test:8971/review?id=1.0-link"


async def test_the_first_summary_time_survives_a_replay(runtime: Runtime) -> None:
    messages = lifecycle("1.0-summary-time")
    await _record(runtime, messages["genai"])
    first = (await _row(runtime, "1.0-summary-time"))["summarised_at"]
    await asyncio.sleep(0.05)
    await _record(runtime, messages["genai"])
    await _record(runtime, messages["end"])

    assert first is not None
    assert (await _row(runtime, "1.0-summary-time"))["summarised_at"] == first


async def test_an_alert_without_a_summary_has_no_summary_time(runtime: Runtime) -> None:
    await _record(runtime, lifecycle("1.0-no-summary")["end"])

    assert (await _row(runtime, "1.0-no-summary"))["summarised_at"] is None
