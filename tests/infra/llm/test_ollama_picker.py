import json

import httpx
import pytest

from vigisift.infra.llm.ollama_picker import OllamaIndicatorPicker
from vigisift.ports.llm import ChatUnavailableError

CHOICES = ["Weapons visible", "Putting a product into a bag", "none"]


def _picker(
    reply: httpx.Response | Exception, sent: list[dict[str, object]]
) -> OllamaIndicatorPicker:
    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if isinstance(reply, Exception):
            raise reply
        return reply

    return OllamaIndicatorPicker(
        "qwen3-vl:4b-instruct", "http://ollama.test", num_ctx=16384,
        transport=httpx.MockTransport(handle),
    )  # fmt: skip


def _answer(indicator: str) -> httpx.Response:
    return httpx.Response(200, json={"response": json.dumps({"indicator": indicator})})


async def test_the_model_can_only_answer_with_one_of_the_choices() -> None:
    sent: list[dict[str, object]] = []

    choice = await _picker(_answer("Putting a product into a bag"), sent).pick("p", CHOICES)

    [body] = sent
    assert choice == "Putting a product into a bag"
    assert body["format"] == {
        "type": "object",
        "properties": {"indicator": {"type": "string", "enum": CHOICES}},
        "required": ["indicator"],
    }
    options = body["options"]
    assert isinstance(options, dict)
    assert options["temperature"] == 0
    assert options["num_ctx"] == 16384, "differing from Frigate makes Ollama reload the model"
    assert body["think"] is False


@pytest.mark.parametrize(
    "reply",
    [
        _answer("Level 2, trust me"),
        httpx.Response(200, json={"response": "not json"}),
        httpx.Response(500),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
    ],
)
async def test_anything_but_a_listed_choice_is_unavailable(
    reply: httpx.Response | Exception,
) -> None:
    with pytest.raises(ChatUnavailableError):
        await _picker(reply, []).pick("p", CHOICES)
