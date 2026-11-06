import json
from typing import Any

import httpx
import pytest

from tests.fakes.http import RecordingTransport
from vigisift.infra.llm.ollama_chat import OllamaChatModel
from vigisift.ports.llm import ChatUnavailableError, ModelTurn, ToolCall, ToolResult

_TOOLS = [{"name": "list_devices", "description": "d", "input_schema": {"type": "object"}}]


def _ndjson(*chunks: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, text="\n".join(json.dumps(c) for c in chunks) + "\n")


async def _collect(model: OllamaChatModel, transcript: list[Any]) -> tuple[str, ModelTurn]:
    text, turn = "", None
    async for item in model.stream("system", transcript, _TOOLS):
        if isinstance(item, str):
            text += item
        else:
            turn = item
    assert turn is not None
    return text, turn


async def test_text_streams_and_usage_comes_from_the_final_chunk() -> None:
    rec = RecordingTransport(
        _ndjson(
            {"message": {"content": "Hello "}},
            {"message": {"content": "there"}},
            {"done": True, "done_reason": "stop", "prompt_eval_count": 120, "eval_count": 8},
        )
    )
    text, turn = await _collect(OllamaChatModel(transport=rec.transport()), ["hi"])

    assert text == "Hello there"
    assert turn.stop == "end"
    assert turn.usage is not None and (turn.usage.input_tokens, turn.usage.output_tokens) == (
        120,
        8,
    )


async def test_tool_calls_end_the_turn_with_tool_use() -> None:
    rec = RecordingTransport(
        _ndjson(
            {
                "message": {
                    "content": "",
                    "tool_calls": [{"function": {"name": "list_devices", "arguments": {}}}],
                }
            },
            {"done": True, "done_reason": "stop"},
        )
    )
    _, turn = await _collect(OllamaChatModel(transport=rec.transport()), ["anything?"])

    assert turn.stop == "tool_use"
    assert turn.tool_calls == [ToolCall("call_0", "list_devices", {})]
    assert rec.bodies[0]["tools"][0]["function"]["name"] == "list_devices", "Ollama's tool format"


async def test_reasoning_is_never_streamed_to_the_user() -> None:
    rec = RecordingTransport(
        _ndjson(
            {"message": {"thinking": "Okay, the user asks about devices..."}},
            {"message": {"content": "There are 3 devices."}},
            {"done": True},
        )
    )

    text, turn = await _collect(OllamaChatModel(transport=rec.transport()), ["?"])

    assert text == "There are 3 devices."
    assert "Okay" not in turn.text


async def test_answer_only_in_the_thinking_field_is_still_read() -> None:
    rec = RecordingTransport(
        _ndjson(
            {"message": {"thinking": "<think>reasoning</think>There are 3 devices."}},
            {"done": True},
        )
    )

    text, _ = await _collect(OllamaChatModel(transport=rec.transport()), ["?"])

    assert text == "There are 3 devices.", "Ollama + qwen3-vl quirk: empty content"


async def test_a_cut_answer_is_max_tokens() -> None:
    rec = RecordingTransport(
        _ndjson({"message": {"content": "abc"}}, {"done": True, "done_reason": "length"})
    )

    _, turn = await _collect(OllamaChatModel(transport=rec.transport()), ["?"])

    assert turn.stop == "max_tokens"


async def test_tool_results_and_images_are_rendered_for_ollama() -> None:
    call = ToolCall("call_0", "get_snapshot", {"camera_id": "cam-1"})
    previous = ModelTurn("", [call], "tool_use")
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"},
    }
    results = [ToolResult(call, [image, {"type": "text", "text": "cam-1 image"}])]
    rec = RecordingTransport(_ndjson({"message": {"content": "ok"}}, {"done": True}))

    await _collect(OllamaChatModel(transport=rec.transport()), ["xem cam", previous, results])

    messages = rec.bodies[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "user"]
    assert messages[2]["tool_calls"] == [
        {"function": {"name": "get_snapshot", "arguments": {"camera_id": "cam-1"}}}
    ]
    assert messages[3] == {"role": "tool", "tool_name": "get_snapshot", "content": "cam-1 image"}
    assert messages[4]["images"] == ["QUJD"], "images a tool returns must reach the vision model"


async def test_a_dead_ollama_is_chat_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ChatUnavailableError):
        await _collect(OllamaChatModel(transport=httpx.MockTransport(refuse)), ["?"])
