from datetime import UTC, datetime

from httpx import AsyncClient

from tests.fakes.ports import FakeFrigateApi
from tests.features.alerts.frigate import START, lifecycle, message, segment
from vigisift.features.alerts import repository
from vigisift.features.alerts.payload import parse_review_message
from vigisift.runtime import Runtime


async def _seed(runtime: Runtime) -> None:
    async with runtime.session() as session:
        await repository.record(
            session, parse_review_message("demo", lifecycle("1.0-quiet")["new"])
        )
        await repository.record(
            session, parse_review_message("demo", lifecycle("2.0-sus")["genai"])
        )


async def test_sites_do_not_leak_the_internal_api_address(client: AsyncClient) -> None:
    [site] = (await client.get("/sites")).json()

    assert site == {"id": "demo", "name": "Demo store", "ui_url": "http://frigate.test:8971"}


async def test_alerts_can_be_narrowed_to_suspicious_ones(
    client: AsyncClient, runtime: Runtime
) -> None:
    await _seed(runtime)

    everything = (await client.get("/alerts", params={"since_minutes": 10080})).json()
    suspicious = (
        await client.get("/alerts", params={"since_minutes": 10080, "min_threat_level": 1})
    ).json()

    assert {a["review_id"] for a in everything} == {"1.0-quiet", "2.0-sus"}
    assert [a["review_id"] for a in suspicious] == ["2.0-sus"]
    assert suspicious[0]["title"] and suspicious[0]["times_approximate"] is True


async def test_old_alerts_fall_outside_the_window(client: AsyncClient, runtime: Runtime) -> None:
    """The sample payload starts an hour ago: a 1-minute window must not see it."""
    await _seed(runtime)

    assert (await client.get("/alerts", params={"since_minutes": 1})).json() == []


async def test_query_bounds_are_enforced(client: AsyncClient) -> None:
    for params in ({"limit": 0}, {"limit": 201}, {"min_threat_level": 3}, {"since_minutes": 0}):
        assert (await client.get("/alerts", params=params)).status_code == 422


async def test_alerts_can_be_narrowed_to_one_camera_and_a_time_range(
    client: AsyncClient, runtime: Runtime
) -> None:
    async with runtime.session() as session:
        for review_id, camera, start in [
            ("1.0-early", "eval_cam", START),
            ("2.0-late", "eval_cam", START + 600),
            ("3.0-other", "shop_aisle", START + 600),
        ]:
            raw = message("new", segment(review_id, camera=camera, start=start))
            await repository.record(session, parse_review_message("demo", raw))

    def at(seconds: float) -> str:
        return datetime.fromtimestamp(seconds, UTC).isoformat()

    rows = (
        await client.get(
            "/alerts",
            params={
                "since_minutes": 10080,
                "camera": "eval_cam",
                "started_after": at(START + 300),
                "started_before": at(START + 900),
            },
        )
    ).json()

    assert [r["review_id"] for r in rows] == ["2.0-late"]
    assert (await client.get("/alerts", params={"camera": "a..b"})).status_code == 422


async def test_a_clip_is_streamed_without_leaving_a_copy_in_the_browser(
    client: AsyncClient, runtime: Runtime, frigate: FakeFrigateApi
) -> None:
    await _seed(runtime)
    frigate.clips["1.0-quiet"] = [b"first-", b"second"]

    response = await client.get("/alerts/demo/1.0-quiet/clip")

    assert response.status_code == 200
    assert response.content == b"first-second"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["cache-control"] == "no-store"


async def test_a_clip_frigate_no_longer_has_is_not_found(
    client: AsyncClient, runtime: Runtime
) -> None:
    await _seed(runtime)

    response = await client.get("/alerts/demo/1.0-quiet/clip")

    assert response.status_code == 404
    assert response.json()["detail"] == "clip_not_available"


async def test_a_clip_with_frigate_down_says_so(
    client: AsyncClient, runtime: Runtime, frigate: FakeFrigateApi
) -> None:
    await _seed(runtime)
    frigate.failing.add("demo")

    response = await client.get("/alerts/demo/1.0-quiet/clip")

    assert response.status_code == 503
    assert response.json()["detail"] == "frigate_unavailable"


async def test_only_clips_of_known_alerts_can_be_fetched(
    client: AsyncClient, frigate: FakeFrigateApi
) -> None:
    """A review Frigate has but VigiSift doesn't must not be reachable through here."""
    frigate.clips["9.9-other"] = [b"secret"]

    response = await client.get("/alerts/demo/9.9-other/clip")

    assert response.status_code == 404
    assert response.json()["detail"] == "alert_not_found"


async def test_clips_need_a_signed_in_operator(anonymous: AsyncClient, runtime: Runtime) -> None:
    await _seed(runtime)

    assert (await anonymous.get("/alerts/demo/1.0-quiet/clip")).status_code == 401
