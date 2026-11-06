from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from vigisift.domain.usage import LlmUsage

Stop = Literal["end", "tool_use", "max_tokens", "refusal"]


class ChatUnavailableError(Exception):
    """The model provider can't answer (network, overload, misconfiguration)."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call: ToolCall
    # Text, or a list of blocks like {"type": "image" | "text", ...}.
    content: str | list[dict[str, Any]]
    is_error: bool = False


@dataclass
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    stop: Stop
    usage: LlmUsage | None = None
    # The provider's original (for example content with Anthropic thinking blocks),
    # so the adapter can send it back intact on the next turn.
    raw: Any = field(default=None, repr=False)


# One conversation entry: the user's question, a model turn, or the results of
# that turn's tools. Provider-neutral: each adapter translates to its own
# API format.
Entry = str | ModelTurn | list[ToolResult]


class ChatModel(Protocol):
    """The way out to a chat model that can call tools.

    Agent knows only this Protocol, so moving from Anthropic to a local model changes
    configuration, not the tool loop, validation or command-confirmation rules.
    """

    name: str
    # Only models billed per token count toward the daily budget.
    paid: bool

    def stream(
        self, system: str, transcript: list[Entry], tools: list[dict[str, Any]]
    ) -> AsyncIterator[str | ModelTurn]:
        """Yield each piece of text as it comes, and exactly one ModelTurn at the end."""
        ...
