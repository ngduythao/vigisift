"""Real payloads recorded from Frigate 0.18.2 with a fake camera (`frigate/demo/reviews`):
one review going the full way new → update → update → end → genai."""

import itertools
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vigisift.features import topics
from vigisift.features.alerts import repository
from vigisift.features.alerts.payload import parse_review_message
from vigisift.runtime import Runtime

FIXTURE = Path(__file__).with_name("fixtures") / "frigate-0.18.2-review.jsonl"
MESSAGES: list[dict[str, Any]] = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
END = next(m["payload"]["after"] for m in MESSAGES if m["payload"]["type"] == "end")
SUMMARY = next(m["payload"]["after"] for m in MESSAGES if m["payload"]["type"] == "genai")


def _with_id(payload: dict[str, Any], review_id: str) -> str:
    copy = json.loads(json.dumps(payload))
    copy["before"]["id"] = copy["after"]["id"] = review_id
    return json.dumps(copy)


def test_every_real_message_parses() -> None:
    for message in MESSAGES:
        site = topics.frigate_site(message["topic"], "reviews")
        update = parse_review_message(site, json.dumps(message["payload"]))
        assert (update.site_id, update.camera) == ("demo", "shop_aisle")


def test_frigate_really_widens_the_times_it_sends_with_a_summary() -> None:
    """Not guessed from source code: these are the numbers on the wire."""
    assert SUMMARY["start_time"] < END["start_time"]
    assert SUMMARY["end_time"] > END["end_time"]


async def test_the_real_lifecycle_in_any_order_ends_in_one_exact_alert(runtime: Runtime) -> None:
    expected = {
        "started_at": datetime.fromtimestamp(END["start_time"], UTC),
        "ended_at": datetime.fromtimestamp(END["end_time"], UTC),
        "times_approximate": False,
        "threat_level": SUMMARY["data"]["metadata"]["potential_threat_level"],
        "title": SUMMARY["data"]["metadata"]["title"],
    }
    orders = list(itertools.permutations(range(len(MESSAGES))))
    for index, order in enumerate(orders):
        review_id = f"{index}.0-real"
        async with runtime.session() as session:
            for position in order:
                raw = _with_id(MESSAGES[position]["payload"], review_id)
                await repository.record(session, parse_review_message("demo", raw))
            [row] = [
                r
                for r in await repository.recent(
                    session, site_id="demo", since_minutes=10**7, min_threat_level=None, limit=200
                )
                if r["review_id"] == review_id
            ]
        assert {key: row[key] for key in expected} == expected, f"order {order}"
