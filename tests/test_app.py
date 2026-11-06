from httpx import AsyncClient

from tests.fakes.ports import FakeMqttClient


async def test_status_goes_to_every_instance_but_telemetry_is_shared(
    client: AsyncClient, mqtt: FakeMqttClient
) -> None:
    groups = {s.topic: s.shared_group for s in mqtt.subscriptions}

    assert groups["device/+/status"] is None, (
        "the broker doesn't send retained to shared subscriptions"
    )
    assert groups["device/+/telemetry"] == "vigisift"
    assert groups["device/+/ack"] == "vigisift"


async def test_the_console_page_is_always_revalidated(anonymous: AsyncClient) -> None:
    """After each deploy, no operator may stay on a cached old UI."""
    response = await anonymous.get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


async def test_the_font_is_public_because_the_sign_in_page_needs_it(anonymous: AsyncClient) -> None:
    response = await anonymous.get("/fonts/inter-latin-wght-normal.woff2")

    assert response.status_code == 200
    assert response.content.startswith(b"wOF2")
