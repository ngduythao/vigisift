import asyncio
import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.features.alerts.frigate import START, SUMMARY, message, segment
from vigisift.features.alerts import repository
from vigisift.features.alerts.payload import parse_review_message
from vigisift.runtime import Runtime


async def _alert(
    runtime: Runtime,
    review_id: str,
    *,
    threat: int | None,
    severity: str = "alert",
    start_offset: float = 0,
) -> None:
    metadata = SUMMARY | {"potential_threat_level": threat} if threat is not None else None
    after = segment(review_id, severity=severity, start=START + start_offset, metadata=metadata)
    async with runtime.session() as session:
        await repository.record(session, parse_review_message("demo", message("new", after)))


async def _decide(http: AsyncClient, review_id: str, verdict: str, **extra: Any) -> Any:
    return await http.put(f"/alerts/demo/{review_id}/verdict", json={"verdict": verdict, **extra})


async def test_the_queue_puts_what_the_ai_found_suspicious_first(
    client: AsyncClient, runtime: Runtime
) -> None:
    await _alert(runtime, "1.0-normal", threat=0, start_offset=30)
    await _alert(runtime, "2.0-pending", threat=None, start_offset=20)
    await _alert(runtime, "3.0-suspicious", threat=1, start_offset=10)
    await _alert(runtime, "4.0-critical", threat=2)
    await _alert(runtime, "5.0-detection", threat=2, severity="detection")

    queue = [a["review_id"] for a in (await client.get("/queue")).json()]

    assert queue == ["4.0-critical", "3.0-suspicious", "2.0-pending", "1.0-normal"], (
        "no summary yet still needs a look, but mustn't hide alerts the AI found suspicious;"
        " detections don't enter the queue"
    )


async def test_a_decided_alert_leaves_everyones_queue(
    client: AsyncClient, admin: AsyncClient, runtime: Runtime
) -> None:
    await _alert(runtime, "1.0-a", threat=1)

    response = await _decide(client, "1.0-a", "suspicious", note="  put box in pocket  ")

    assert response.status_code == 200
    assert (await admin.get("/queue")).json() == [], "other operators no longer see it either"
    [alert] = (await client.get("/alerts", params={"since_minutes": 10080})).json()
    assert (alert["verdict"], alert["verdict_by"]) == ("suspicious", "anna")


async def test_who_decided_comes_from_the_session_not_the_request(
    client: AsyncClient, runtime: Runtime
) -> None:
    await _alert(runtime, "1.0-a", threat=1)

    await _decide(client, "1.0-a", "normal", operator_id=2, by="boss")

    [alert] = (await client.get("/alerts", params={"since_minutes": 10080})).json()
    assert alert["verdict_by"] == "anna"


async def test_an_operator_can_change_their_mind(client: AsyncClient, runtime: Runtime) -> None:
    await _alert(runtime, "1.0-a", threat=1)

    await _decide(client, "1.0-a", "normal")
    await _decide(client, "1.0-a", "suspicious")

    async with runtime.session() as session:
        rows = (await session.execute(text("SELECT verdict FROM alert_verdicts"))).all()
    assert [r.verdict for r in rows] == ["suspicious"], "one verdict per operator, overwritten"


async def test_the_verdict_records_what_the_ai_had_said_at_that_moment(
    client: AsyncClient, runtime: Runtime
) -> None:
    await _alert(runtime, "1.0-a", threat=None)
    await _decide(client, "1.0-a", "suspicious")
    await _alert(runtime, "1.0-a", threat=0)  # the summary arrives AFTER the operator judged

    async with runtime.session() as session:
        snapshot = (
            await session.execute(text("SELECT ai_threat_level FROM alert_verdicts"))
        ).scalar_one()
    assert snapshot is None, "judged before any summary: the AI can't have led the operator"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"verdict": "maybe"}, 422),
        ({"verdict": "normal", "note": "x" * 501}, 422),
    ],
)
async def test_bad_verdicts_are_refused(
    client: AsyncClient, runtime: Runtime, body: dict[str, Any], status: int
) -> None:
    await _alert(runtime, "1.0-a", threat=1)

    assert (await client.put("/alerts/demo/1.0-a/verdict", json=body)).status_code == status


async def test_a_verdict_for_a_missing_alert_is_404(client: AsyncClient) -> None:
    assert (await _decide(client, "9.9-ghost", "normal")).status_code == 404


async def test_other_operators_hear_about_the_verdict(
    client: AsyncClient, runtime: Runtime
) -> None:
    await _alert(runtime, "1.0-a", threat=1)

    with runtime.hub.subscribe() as browser:
        await _decide(client, "1.0-a", "normal")
        event = json.loads(await asyncio.wait_for(browser.get(), timeout=3))

    assert event == {
        "type": "alert_verdict",
        "site_id": "demo",
        "review_id": "1.0-a",
        "verdict": "normal",
        "by": "anna",
    }


async def test_metrics_compare_the_ai_with_the_operators(
    client: AsyncClient, runtime: Runtime
) -> None:
    # AI suspicious + operator suspicious (right), AI suspicious + operator normal
    # (false alarm), AI normal + operator suspicious (missed), not judged, unclear.
    cases = [("1.0", 1, "suspicious"), ("2.0", 2, "normal"), ("3.0", 0, "suspicious")]
    for review_id, threat, verdict in cases:
        await _alert(runtime, review_id, threat=threat)
        await _decide(client, review_id, verdict)
    await _alert(runtime, "4.0", threat=1)
    await _alert(runtime, "5.0", threat=1)
    await _decide(client, "5.0", "unclear")

    [site] = (await client.get("/review-metrics", params={"days": 1})).json()

    assert (site["alerts"], site["reviewed"], site["ai_flagged"]) == (5, 4, 4)
    assert site["precision"] == 0.5, "1 right of 2 clearly judged alerts the AI flagged"
    assert site["recall"] == 0.5, "the AI caught 1 of 2 really suspicious alerts"
    assert site["false_alarms_per_hour"] == pytest.approx(1 / 24)
    assert site["median_seconds_to_review"] is not None


async def test_only_an_admin_can_export_labels(
    client: AsyncClient, admin: AsyncClient, runtime: Runtime
) -> None:
    await _alert(runtime, "1.0-a", threat=1)
    await _decide(client, "1.0-a", "suspicious")

    assert (await client.get("/verdicts/export")).status_code == 403
    response = await admin.get("/verdicts/export")

    [line] = response.text.splitlines()
    label = json.loads(line)
    assert (label["review_id"], label["verdict"], label["ai_threat_level"]) == (
        "1.0-a",
        "suspicious",
        1,
    )


async def test_changing_a_verdict_does_not_make_the_review_look_slower(
    client: AsyncClient, runtime: Runtime
) -> None:
    await _alert(runtime, "1.0-a", threat=1)
    await _decide(client, "1.0-a", "normal")
    async with runtime.session() as session:
        await session.execute(
            text("UPDATE camera_alerts SET first_seen_at = now() - interval '10 minutes';")
        )
        await session.execute(
            text("UPDATE alert_verdicts SET first_decided_at = now() - interval '5 minutes'")
        )
    await _decide(client, "1.0-a", "suspicious")  # changes their mind later

    [site] = (await client.get("/review-metrics")).json()
    assert site["median_seconds_to_review"] == pytest.approx(300, abs=5)


async def test_the_latest_operator_wins_the_label(
    client: AsyncClient, admin: AsyncClient, runtime: Runtime
) -> None:
    await _alert(runtime, "1.0-a", threat=1)
    await _decide(client, "1.0-a", "normal")
    await _decide(admin, "1.0-a", "suspicious")

    [label] = [json.loads(line) for line in (await admin.get("/verdicts/export")).text.splitlines()]
    assert label["verdict"] == "suspicious"


async def test_the_queue_can_be_narrowed_to_one_site(client: AsyncClient, runtime: Runtime) -> None:
    await _alert(runtime, "1.0-a", threat=1)

    assert (await client.get("/queue", params={"site_id": "other"})).json() == []
    assert len((await client.get("/queue", params={"site_id": "demo"})).json()) == 1
