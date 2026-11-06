from tests.fakes.anthropic import FakeAnthropicClient, say
from vigisift.infra.llm.anthropic_chat import AnthropicChatModel

_TOOLS = [{"name": "noop", "description": "does nothing", "input_schema": {"type": "object"}}]


async def _drain(llm: FakeAnthropicClient) -> None:
    async for _ in AnthropicChatModel(llm).stream("system", ["hello"], _TOOLS):
        pass


async def test_the_default_model_opts_into_server_side_fallback() -> None:
    llm = FakeAnthropicClient(say("ok"))

    await _drain(llm)

    assert llm.used_beta == [True]
    assert llm.requests[0]["fallbacks"] == "default"


async def test_tools_and_system_are_cached_together() -> None:
    llm = FakeAnthropicClient(say("ok"))

    await _drain(llm)

    [system] = llm.requests[0]["system"]
    assert system["cache_control"] == {"type": "ephemeral"}
