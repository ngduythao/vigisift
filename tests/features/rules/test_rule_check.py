import asyncio
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text

from tests.fakes.ports import FakeFrigateApi, FakeIndicatorPicker
from tests.features.alerts.frigate import START, SUMMARY, message, segment
from vigisift.features.alerts import repository
from vigisift.features.alerts.payload import parse_review_message
from vigisift.features.rules.check import check_pending
from vigisift.features.rules.sync import RuleSync
from vigisift.runtime import Runtime

POCKET = "A person putting a product into their own pocket or bag"
RULES: dict[str, Any] = {
    "scene": "an aisle of a small shop",
    "normal": ["A person looking at products and putting them back"],
    "suspicious": [POCKET],
    "critical": ["Weapons visible"],
    "concerns": [],
}
# Frigate describes the goods-in-pocket scene correctly but scores it 0: exactly what the eval saw.
CALM = SUMMARY | {"potential_threat_level": 0}
# A really normal summary: nothing matches a level 1 rule.
BROWSING = CALM | {
    "shortSummary": "A person looks at an item and puts it back on the shelf, taking nothing.",
    "scene": "A person looks at goods in the aisle and returns the item to the shelf as before. "
    * 3,
    "observations": ["The person looks at an item.", "The person puts it back on the shelf."],
}


async def _summarised(runtime: Runtime, review_id: str, summary: dict[str, Any] = CALM) -> None:
    raw = message("genai", segment(review_id, severity="alert", end=START + 12, metadata=summary))
    async with runtime.session() as session:
        await repository.record(session, parse_review_message("demo", raw))


async def _alert(client: AsyncClient, review_id: str) -> dict[str, Any]:
    rows = (await client.get("/alerts", params={"since_minutes": 10080})).json()
    [alert] = [a for a in rows if a["review_id"] == review_id]
    return dict(alert)


def _picker() -> FakeIndicatorPicker:
    return FakeIndicatorPicker({"jacket pocket": POCKET})


async def test_a_calm_summary_that_matches_a_rule_is_raised_to_that_level(
    admin: AsyncClient, runtime: Runtime
) -> None:
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    await _summarised(runtime, "1.0-pocket")

    with runtime.hub.subscribe() as browser:
        assert await check_pending(runtime.session, _picker(), runtime.events) == 1
        await asyncio.sleep(0.3)
        pushed = browser.qsize()

    alert = await _alert(admin, "1.0-pocket")
    assert (alert["threat_level"], alert["rule_level"], alert["level"]) == (0, 1, 1)
    assert alert["rule_match"] == POCKET
    assert alert["observations"] == SUMMARY["observations"]
    assert pushed == 1, "operators must see the new level right away"
    suspicious = (
        await admin.get("/alerts", params={"since_minutes": 10080, "min_threat_level": 1})
    ).json()
    assert [a["review_id"] for a in suspicious] == ["1.0-pocket"]


async def test_the_model_never_lowers_what_frigate_already_flagged(
    admin: AsyncClient, runtime: Runtime
) -> None:
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    await _summarised(runtime, "1.0-flagged", SUMMARY)

    await check_pending(runtime.session, FakeIndicatorPicker(), runtime.events)

    alert = await _alert(admin, "1.0-flagged")
    assert (alert["threat_level"], alert["rule_level"], alert["level"]) == (1, 0, 1)
    assert alert["rule_match"] is None


async def test_cameras_without_rules_are_left_alone(runtime: Runtime) -> None:
    await _summarised(runtime, "1.0-no-rules")
    picker = _picker()

    assert await check_pending(runtime.session, picker, runtime.events) == 0
    assert picker.prompts == []


async def test_a_model_outage_leaves_the_alert_for_the_next_pass(
    admin: AsyncClient, runtime: Runtime
) -> None:
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    await _summarised(runtime, "1.0-later")
    picker = _picker()
    picker.failing = True

    assert await check_pending(runtime.session, picker, runtime.events) == 0
    assert (await _alert(admin, "1.0-later"))["rule_level"] is None

    picker.failing = False
    assert await check_pending(runtime.session, picker, runtime.events) == 1
    assert (await _alert(admin, "1.0-later"))["rule_level"] == 1


async def test_two_instances_never_check_the_same_alert_twice(
    admin: AsyncClient, runtime: Runtime
) -> None:
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    for index in range(3):
        await _summarised(runtime, f"{index}.0-shared")
    picker = _picker()
    picker.delay_sec = 0.1

    done = await asyncio.gather(
        check_pending(runtime.session, picker, runtime.events),
        check_pending(runtime.session, picker, runtime.events),
    )

    assert sum(done) == 3
    assert len(picker.prompts) == 3


async def test_a_new_summary_is_checked_again_but_a_replay_is_not(
    admin: AsyncClient, runtime: Runtime
) -> None:
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    await _summarised(runtime, "1.0-resummarised")
    picker = _picker()
    await check_pending(runtime.session, picker, runtime.events)

    await _summarised(runtime, "1.0-resummarised")
    assert await check_pending(runtime.session, picker, runtime.events) == 0, "same summary"

    await _summarised(runtime, "1.0-resummarised", BROWSING)
    assert (await _alert(admin, "1.0-resummarised"))["rule_level"] is None
    assert await check_pending(runtime.session, picker, runtime.events) == 1
    assert (await _alert(admin, "1.0-resummarised"))["level"] == 0


async def test_the_review_queue_puts_rule_flagged_alerts_first(
    admin: AsyncClient, runtime: Runtime
) -> None:
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    await _summarised(runtime, "2.0-newer-calm", BROWSING)
    await _summarised(runtime, "1.0-pocket")
    await check_pending(runtime.session, _picker(), runtime.events)

    queue = (await admin.get("/queue", params={"since_minutes": 10080})).json()

    assert [a["review_id"] for a in queue][:1] == ["1.0-pocket"]


async def test_rules_written_under_old_system_guidance_are_rebuilt_and_pushed(
    admin: AsyncClient, runtime: Runtime, frigate: FakeFrigateApi
) -> None:
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    async with runtime.session() as session:
        # As after a release that changes the system's fixed prompt part.
        await session.execute(text("UPDATE camera_rules SET prompt = 'old prompt', sha = 'old'"))

    await RuleSync(runtime.session, frigate, runtime.cooldown, 300).run_once(force=True)

    [rules] = (await admin.get("/rules")).json()
    assert rules["prompt"] != "old prompt" and rules["applied"] is True
    assert frigate.rules[("demo", "shop_aisle")][0] == rules["prompt"]


async def test_an_alert_raised_by_its_rules_leads_the_queue_and_counts_as_flagged(
    admin: AsyncClient, runtime: Runtime
) -> None:
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    await _summarised(runtime, "1.0-raised")
    await _summarised(runtime, "2.0-browsing", BROWSING)
    await check_pending(runtime.session, _picker(), runtime.events)

    queue = [a["review_id"] for a in (await admin.get("/queue")).json()]
    assert queue[0] == "1.0-raised", "the queue orders by the effective level"
    await admin.put("/alerts/demo/1.0-raised/verdict", json={"verdict": "suspicious"})
    [site] = (await admin.get("/review-metrics")).json()
    assert (site["ai_flagged"], site["recall"]) == (1, 1.0)
