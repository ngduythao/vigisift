import asyncio
import json

from httpx import ASGITransport, AsyncClient

from tests.fakes.ports import FakeFrigateApi, FakeMqttClient
from vigisift.app import create_app
from vigisift.config import Settings
from vigisift.runtime import Runtime


async def test_telemetry_handled_by_instance_b_reaches_a_browser_on_instance_a(
    runtime: Runtime, settings: Settings
) -> None:
    """Two API instances behind one load balancer, sharing Redis.

    `client` is instance A (devices seeded). Instance B receives telemetry from the
    broker; a browser connected to A must still see it.
    """
    mqtt_b = FakeMqttClient()
    app_b = create_app(settings, mqtt_b, frigate=FakeFrigateApi())
    hub_a = runtime.hub

    async with (
        AsyncClient(transport=ASGITransport(app=app_b), base_url="http://b"),
        app_b.router.lifespan_context(app_b),
    ):
        with hub_a.subscribe() as browser_on_a:
            mqtt_b.deliver("device/fan-1/telemetry", json.dumps({"state": "ON"}))
            message = await asyncio.wait_for(browser_on_a.get(), timeout=3)

    assert json.loads(message) == {"type": "telemetry", "device_id": "fan-1", "state": "ON"}
