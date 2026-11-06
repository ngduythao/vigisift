import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import login
from tests.fakes.anthropic import FakeAnthropicClient, call, say
from tests.fakes.ports import FakeFrigateApi, FakeMqttClient
from vigisift.app import create_app
from vigisift.config import Settings
from vigisift.infra.llm.anthropic_chat import AnthropicChatModel


@pytest.fixture
async def assistant(
    settings: Settings, mqtt: FakeMqttClient, client: AsyncClient
) -> AsyncIterator[tuple[AsyncClient, FakeAnthropicClient]]:
    llm = FakeAnthropicClient(
        call(("t1", "send_command", {"device_id": "lamp-1", "action": "OFF"})),
        say("I've created a request to turn off the light, please click confirm."),
    )
    app = create_app(settings, mqtt, AnthropicChatModel(llm), frigate=FakeFrigateApi())
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
        app.router.lifespan_context(app),
    ):
        await login(http, "anna")
        yield http, llm


def _sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for chunk in body.strip().split("\n\n"):
        kind, data = chunk.split("\n", 1)
        events.append((kind.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


async def test_the_agent_only_proposes_a_command(
    assistant: tuple[AsyncClient, FakeAnthropicClient], mqtt: FakeMqttClient
) -> None:
    http, _ = assistant

    response = await http.post(
        "/assistant/chat", json={"message": "turn off the living room light"}
    )

    events = _sse(response.text)
    assert response.headers["content-type"].startswith("text/event-stream")
    confirmation = next(data for kind, data in events if kind == "confirmation")
    assert confirmation["summary"].startswith("Turn off")
    assert mqtt.published == [], "no command is sent until someone confirms"


async def test_confirming_sends_the_command_exactly_once(
    assistant: tuple[AsyncClient, FakeAnthropicClient], mqtt: FakeMqttClient
) -> None:
    http, _ = assistant
    events = _sse((await http.post("/assistant/chat", json={"message": "turn off the light"})).text)
    confirmation = next(data for kind, data in events if kind == "confirmation")

    first = await http.post(f"/assistant/confirmations/{confirmation['id']}")
    second = await http.post(f"/assistant/confirmations/{confirmation['id']}")

    assert first.status_code == 200 and first.json()["action"] == "OFF"
    assert second.status_code == 404, "a confirmation works only once"
    assert [topic for topic, _ in mqtt.published] == ["device/lamp-1/command"]


async def test_assistant_calls_are_in_the_cost_ledger(
    assistant: tuple[AsyncClient, FakeAnthropicClient],
) -> None:
    http, _ = assistant
    await http.post("/assistant/chat", json={"message": "turn off the light"})

    [line] = (await http.get("/llm/usage")).json()
    assert (line["purpose"], line["calls"]) == ("assistant", 2)


async def test_without_a_client_the_assistant_is_disabled(client: AsyncClient) -> None:
    response = await client.post("/assistant/chat", json={"message": "hello"})

    assert response.status_code == 503
