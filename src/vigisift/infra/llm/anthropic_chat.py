from collections.abc import AsyncIterator
from typing import Any

import anthropic

from vigisift.infra.llm.usage import usage_from_anthropic
from vigisift.ports.llm import ChatUnavailableError, Entry, ModelTurn, Stop, ToolCall

# Models with server-side fallback on a policy refusal: the API reruns the
# request on a backup model within the same call.
_FALLBACK_MODELS = frozenset({"claude-opus-5", "claude-fable-5-1"})
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_STOPS: dict[str, Stop] = {
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def _render(transcript: list[Entry]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for entry in transcript:
        if isinstance(entry, str):
            messages.append({"role": "user", "content": entry})
        elif isinstance(entry, ModelTurn):
            # Send back the WHOLE content (thinking and tool_use included), not only
            # the text: the API needs to see the previous turn to match tool_result.
            messages.append({"role": "assistant", "content": entry.raw})
        else:
            # All of a turn's tool_results go in ONE message; splitting them
            # teaches the model to stop calling tools in parallel.
            blocks = []
            for result in entry:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": result.call.id,
                    "content": result.content,
                }
                if result.is_error:
                    block["is_error"] = True
                blocks.append(block)
            messages.append({"role": "user", "content": blocks})
    return messages


class AnthropicChatModel:
    paid = True

    def __init__(self, client: Any, model: str = "claude-opus-5") -> None:
        self._client = client
        self.name = model

    def _open(self, system: str, transcript: list[Entry], tools: list[dict[str, Any]]) -> Any:
        params: dict[str, Any] = {
            "model": self.name,
            "max_tokens": 16000,
            # tools render before system, so a breakpoint at system caches both.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": tools,
            "messages": _render(transcript),
        }
        if self.name in _FALLBACK_MODELS:
            return self._client.beta.messages.stream(
                **params, betas=[_FALLBACK_BETA], fallbacks="default"
            )
        return self._client.messages.stream(**params)

    async def stream(
        self, system: str, transcript: list[Entry], tools: list[dict[str, Any]]
    ) -> AsyncIterator[str | ModelTurn]:
        try:
            async with self._open(system, transcript, tools) as stream:
                async for chunk in stream.text_stream:
                    yield chunk
                final = await stream.get_final_message()
        except anthropic.APIError as error:
            raise ChatUnavailableError(type(error).__name__) from error

        yield ModelTurn(
            text="".join(b.text for b in final.content if b.type == "text"),
            tool_calls=[
                ToolCall(b.id, b.name, dict(b.input or {}))
                for b in final.content
                if b.type == "tool_use"
            ],
            stop=_STOPS.get(final.stop_reason, "end"),
            usage=usage_from_anthropic(self.name, final.usage),
            raw=final.content,
        )
