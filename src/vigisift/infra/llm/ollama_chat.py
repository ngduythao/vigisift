import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from vigisift.domain.usage import LlmUsage
from vigisift.ports.llm import ChatUnavailableError, Entry, ModelTurn, ToolCall


def _tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-style tool definition → Ollama function style. Same JSON Schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]


def _render(system: str, transcript: list[Entry]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for entry in transcript:
        if isinstance(entry, str):
            messages.append({"role": "user", "content": entry})
        elif isinstance(entry, ModelTurn):
            messages.append(
                {
                    "role": "assistant",
                    "content": entry.text,
                    "tool_calls": [
                        {"function": {"name": c.name, "arguments": c.input}}
                        for c in entry.tool_calls
                    ],
                }
            )
        else:
            images: list[str] = []
            for result in entry:
                if isinstance(result.content, str):
                    text = result.content
                else:
                    text = " ".join(b["text"] for b in result.content if b["type"] == "text")
                    images += [b["source"]["data"] for b in result.content if b["type"] == "image"]
                prefix = "ERROR: " if result.is_error else ""
                messages.append(
                    {"role": "tool", "tool_name": result.call.name, "content": prefix + text}
                )
            # A "tool" role message carries only text; images returned by a tool go
            # in a user message right after, so vision models can see them.
            if images:
                messages.append(
                    {"role": "user", "content": "Image returned by the tool.", "images": images}
                )
    return messages


class OllamaChatModel:
    """A model running locally through Ollama `/api/chat`: no per-token cost.

    In exchange, small models call tools less reliably, so Toolbox's argument
    validation and Agent's turn limit matter even more.
    """

    paid = False

    def __init__(
        self,
        model: str = "qwen3-vl:8b-instruct",
        host: str = "http://127.0.0.1:11434",
        think: bool = False,
        timeout_sec: float = 120,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = model
        self._host = host.rstrip("/")
        self._think = think
        self._timeout = timeout_sec
        self._transport = transport

    async def stream(
        self, system: str, transcript: list[Entry], tools: list[dict[str, Any]]
    ) -> AsyncIterator[str | ModelTurn]:
        body = {
            "model": self.name,
            "messages": _render(system, transcript),
            "tools": _tools(tools),
            "stream": True,
            # Long thinking makes chat answers several times slower on a personal machine.
            "think": self._think,
        }
        text: list[str] = []
        thinking: list[str] = []
        calls: list[ToolCall] = []
        done: dict[str, Any] = {}
        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client,
                client.stream("POST", f"{self._host}/api/chat", json=body) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise ChatUnavailableError(str(chunk["error"]))
                    message = chunk.get("message", {})
                    # Internal reasoning (the `thinking` field) is never streamed to
                    # the user; only `content` is the answer.
                    if message.get("thinking"):
                        thinking.append(message["thinking"])
                    if message.get("content"):
                        text.append(message["content"])
                        yield message["content"]
                    for call in message.get("tool_calls") or []:
                        function = call["function"]
                        arguments = function.get("arguments") or {}
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments or "{}")
                        calls.append(
                            ToolCall(
                                call.get("id") or f"call_{len(calls)}", function["name"], arguments
                            )
                        )
                    if chunk.get("done"):
                        done = chunk
        except httpx.HTTPError as error:
            raise ChatUnavailableError(f"ollama: {error}") from error

        # Ollama + qwen3-vl quirk: sometimes the whole answer is in `thinking` and
        # `content` is empty. Only then take what follows the last </think> tag.
        if not text and not calls and thinking:
            fallback = "".join(thinking).rsplit("</think>", 1)[-1].replace("<think>", "").strip()
            if fallback:
                text.append(fallback)
                yield fallback

        yield ModelTurn(
            text="".join(text),
            tool_calls=calls,
            stop="tool_use"
            if calls
            else "max_tokens"
            if done.get("done_reason") == "length"
            else "end",
            usage=LlmUsage(
                model=f"ollama:{self.name}",
                input_tokens=int(done.get("prompt_eval_count") or 0),
                output_tokens=int(done.get("eval_count") or 0),
            ),
        )
