import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.fakes.ports import FakeFrigateApi
from vigisift.features.rules import repository
from vigisift.features.rules.apply import push_pending
from vigisift.features.rules.sync import RuleSync
from vigisift.ports.frigate import FrigateSite
from vigisift.runtime import Runtime

RULES: dict[str, Any] = {
    "scene": "the stock room of a bakery warehouse",
    "normal": ["Staff stacking boxes"],
    "suspicious": ["Eating stock from the shelves", "Putting cash into a pocket"],
    "critical": ["Forcing the safe"],
    "concerns": ["cash box left open"],
}


async def test_saving_rules_pushes_the_prompt_to_that_cameras_frigate(
    admin: AsyncClient, frigate: FakeFrigateApi
) -> None:
    response = await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)

    assert response.status_code == 200, response.text
    view = response.json()
    assert view["applied"] is True and view["apply_error"] is None
    prompt, concerns = frigate.rules[("demo", "shop_aisle")]
    assert "- Eating stock from the shelves" in prompt
    assert concerns == ["cash box left open"]
    assert view["updated_by"] == "boss"


async def test_only_an_admin_can_change_rules(client: AsyncClient) -> None:
    response = await client.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)

    assert response.status_code == 403


async def test_rules_survive_a_frigate_outage_and_are_pushed_later(
    admin: AsyncClient, frigate: FakeFrigateApi, runtime: Runtime
) -> None:
    frigate.failing.add("demo")

    saved = (await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)).json()
    assert saved["applied"] is False, "Frigate off: rules are saved anyway, waiting to push"

    frigate.failing.clear()
    await RuleSync(runtime.session, frigate, runtime.cooldown, 300).run_once(force=True)

    [rules] = (await admin.get("/rules")).json()
    assert rules["applied"] is True
    assert ("demo", "shop_aisle") in frigate.rules


async def test_a_camera_frigate_does_not_know_is_reported_not_hidden(
    admin: AsyncClient,
) -> None:
    view = (await admin.put("/sites/demo/cameras/back_door/rules", json=RULES)).json()

    assert view["applied"] is False
    assert "back_door" in view["apply_error"]


async def test_editing_rules_during_a_push_never_marks_the_old_version_applied(
    admin: AsyncClient, runtime: Runtime
) -> None:
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    async with runtime.session() as session:
        # Simulates: an admin edits again while the old version is being pushed.
        await session.execute(text("UPDATE camera_rules SET sha = 'newer'"))

    [rules] = (await admin.get("/rules")).json()
    assert rules["applied"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"normal": []},
        {"suspicious": ["x"]},
        {"scene": "y" * 301},
    ],
)
async def test_invalid_rules_are_refused(admin: AsyncClient, change: dict[str, Any]) -> None:
    response = await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES | change)

    assert response.status_code == 422


async def test_unknown_site_and_bad_camera_names(admin: AsyncClient) -> None:
    assert (await admin.put("/sites/ghost/cameras/a/rules", json=RULES)).status_code == 404
    assert (await admin.put("/sites/demo/cameras/a..b/rules", json=RULES)).status_code == 422


async def test_presets_are_ready_to_use(admin: AsyncClient) -> None:
    presets = (await admin.get("/rule-presets")).json()

    assert {"retail_store", "warehouse", "jewelry_store"} <= set(presets)
    response = await admin.put(
        "/sites/demo/cameras/shop_aisle/rules", json=presets["jewelry_store"]
    )
    assert response.status_code == 200


async def test_rules_changed_behind_our_back_in_frigate_are_put_back(
    admin: AsyncClient, frigate: FakeFrigateApi, runtime: Runtime
) -> None:
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    expected = frigate.rules[("demo", "shop_aisle")]
    # Someone restores an old Frigate config.yml.
    frigate.rules[("demo", "shop_aisle")] = ("old prompt", [])

    await RuleSync(runtime.session, frigate, runtime.cooldown, 300).run_once(force=True)

    assert frigate.rules[("demo", "shop_aisle")] == expected


def _sync(runtime: Runtime, frigate: FakeFrigateApi) -> RuleSync:
    return RuleSync(runtime.session, frigate, runtime.cooldown, 300)


async def test_a_rejected_version_is_not_pushed_again_until_the_admin_saves(
    admin: AsyncClient, frigate: FakeFrigateApi, runtime: Runtime
) -> None:
    await admin.put("/sites/demo/cameras/back_door/rules", json=RULES)
    await _sync(runtime, frigate).run_once(force=True)

    assert frigate.pushes.count(("demo", "back_door")) == 1, "Frigate already rejected this version"

    frigate.cameras.add("back_door")  # an admin adds the camera to Frigate and saves again
    view = (await admin.put("/sites/demo/cameras/back_door/rules", json=RULES)).json()

    assert view["applied"] is True and view["apply_error"] is None


async def test_two_pushes_at_once_send_each_version_only_once(
    admin: AsyncClient, frigate: FakeFrigateApi, runtime: Runtime
) -> None:
    frigate.failing.add("demo")
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    frigate.failing.clear()
    frigate.pushes.clear()
    frigate.push_delay_sec = 0.2
    site = FrigateSite("demo", "http://frigate:5000")

    results = await asyncio.gather(
        push_pending(runtime.session, frigate, site), push_pending(runtime.session, frigate, site)
    )

    assert sorted(results) == [0, 1]
    assert frigate.pushes == [("demo", "shop_aisle")]


async def test_a_camera_frigate_cannot_reach_does_not_hold_back_the_others(
    admin: AsyncClient, frigate: FakeFrigateApi, runtime: Runtime
) -> None:
    frigate.failing.add("demo")
    frigate.cameras.add("back_door")
    await admin.put("/sites/demo/cameras/back_door/rules", json=RULES)
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    frigate.failing.clear()
    frigate.unreachable_cameras = {"back_door"}  # sorts before shop_aisle

    pushed = await push_pending(runtime.session, frigate, FrigateSite("demo", "http://f:5000"))

    assert pushed == 1
    assert ("demo", "shop_aisle") in frigate.rules


async def test_a_drift_check_never_unmarks_a_newer_version(
    admin: AsyncClient, runtime: Runtime
) -> None:
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    async with runtime.session() as session:
        await repository.forget_applied(session, "demo", "shop_aisle", "some-older-sha")

    [rules] = (await admin.get("/rules")).json()
    assert rules["applied"] is True
