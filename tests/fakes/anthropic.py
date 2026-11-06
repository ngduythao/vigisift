"""A fake Anthropic client keeping the SDK's shape: `messages.stream` and
`beta.messages.stream` (async context managers with `text_stream` and
`get_final_message()`) for the agent. Answers are scripted up front
and returned one by one in call order."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Block:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: Any = None


@dataclass
class Usage:
    input_tokens: int = 500
    output_tokens: int = 40
    cache_read_input_tokens: int | None = 0
    cache_creation_input_tokens: int | None = 0


@dataclass
class Message:
    content: list[Block]
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)


def say(text: str, stop_reason: str = "end_turn") -> Message:
    return Message([Block("text", text=text)], stop_reason)


def call(*uses: tuple[str, str, dict[str, Any]], stop_reason: str = "tool_use") -> Message:
    return Message(
        [Block("tool_use", id=tool_id, name=name, input=args) for tool_id, name, args in uses],
        stop_reason,
    )


class _Stream:
    def __init__(self, message: Message) -> None:
        self._message = message

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    @property
    def text_stream(self) -> AsyncIterator[str]:
        async def chunks() -> AsyncIterator[str]:
            for block in self._message.content:
                if block.type == "text" and block.text:
                    for word in block.text.split(" "):
                        yield word + " "

        return chunks()

    async def get_final_message(self) -> Message:
        return self._message


class _Messages:
    def __init__(self, owner: "FakeAnthropicClient", beta: bool) -> None:
        self._owner = owner
        self._beta = beta

    def stream(self, **kwargs: Any) -> _Stream:
        return _Stream(self._owner.reply(kwargs, self._beta))


class FakeAnthropicClient:
    def __init__(self, *replies: Message) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.used_beta: list[bool] = []
        self.messages = _Messages(self, beta=False)
        self.beta = type("Beta", (), {"messages": _Messages(self, beta=True)})()

    def reply(self, request: dict[str, Any], beta: bool) -> Message:
        # Capture messages at call time: the agent keeps appending to the list.
        self.requests.append({**request, "messages": list(request["messages"])})
        self.used_beta.append(beta)
        return self.replies.pop(0)
