import json
from typing import Any

from tests.fakes.anthropic import FakeAnthropicClient, call, say
from tests.fakes.ports import (
    FakeTokenBudget,
)
from vigisift.domain.usage import LlmUsage
from vigisift.features.assistant.agent import Agent, AgentEvent
from vigisift.features.assistant.tools import Toolbox
from vigisift.infra.llm.anthropic_chat import AnthropicChatModel


async def _run(agent: Agent, question: str = "anything new?") -> list[AgentEvent]:
    return [event async for event in agent.run(question)]


def _agent(llm: FakeAnthropicClient, tools: Toolbox, **kwargs: Any) -> tuple[Agent, list[LlmUsage]]:
    usages: list[LlmUsage] = []

    async def sink(usage: LlmUsage) -> None:
        usages.append(usage)

    model = AnthropicChatModel(llm)
    return Agent(model, tools, kwargs.pop("budget", FakeTokenBudget()), sink, **kwargs), usages


async def test_a_plain_answer_streams_and_finishes(toolbox: Toolbox) -> None:
    llm = FakeAnthropicClient(say("Everything is fine."))
    agent, usages = _agent(llm, toolbox)

    events = await _run(agent)

    assert (
        "".join(e.data["text"] for e in events if e.kind == "text").strip() == "Everything is fine."
    )
    assert events[-1] == AgentEvent("done", {"reason": "end"})
    assert len(usages) == 1


async def test_the_server_runs_the_tool_and_feeds_the_result_back(toolbox: Toolbox) -> None:
    llm = FakeAnthropicClient(call(("t1", "list_devices", {})), say("There are 3 devices."))
    agent, _ = _agent(llm, toolbox)

    await _run(agent)

    follow_up = llm.requests[1]["messages"]
    [result] = follow_up[-1]["content"]
    assert follow_up[-1]["role"] == "user"
    assert result["tool_use_id"] == "t1", "tool_result must match the tool_use id"
    assert {d["id"] for d in json.loads(result["content"])} == {"lamp-1", "fan-1", "plug-1"}


async def test_parallel_calls_come_back_in_one_message(toolbox: Toolbox) -> None:
    llm = FakeAnthropicClient(
        call(
            ("a", "list_devices", {}),
            ("b", "send_command", {"device_id": "lamp-1", "action": "OFF"}),
        ),
        say("Xong."),
    )
    agent, _ = _agent(llm, toolbox)

    await _run(agent)

    last = llm.requests[1]["messages"][-1]
    assert [block["tool_use_id"] for block in last["content"]] == ["a", "b"]


async def test_bad_arguments_go_back_to_the_model_as_an_error(toolbox: Toolbox) -> None:
    llm = FakeAnthropicClient(
        call(("t1", "send_command", {"device_id": "lamp-1", "action": "BLINK"})), say("Sorry.")
    )
    agent, _ = _agent(llm, toolbox)

    events = await _run(agent)

    [result] = llm.requests[1]["messages"][-1]["content"]
    assert result["is_error"] is True
    assert (
        AgentEvent(
            "tool",
            {
                "name": "send_command",
                "input": {"device_id": "lamp-1", "action": "BLINK"},
                "ok": False,
            },
        )
        in events
    )


async def test_a_looping_model_is_stopped(toolbox: Toolbox) -> None:
    llm = FakeAnthropicClient(*(call((f"t{i}", "list_devices", {})) for i in range(3)))
    agent, _ = _agent(llm, toolbox, max_turns=3)

    events = await _run(agent)

    assert events[-1].kind == "error"
    assert len(llm.requests) == 3


async def test_a_truncated_tool_call_is_never_run(toolbox: Toolbox) -> None:
    llm = FakeAnthropicClient(
        call(("t1", "send_command", {"device_id": "lamp-1"}), stop_reason="max_tokens")
    )
    agent, _ = _agent(llm, toolbox)

    events = await _run(agent)

    assert [e.kind for e in events] == ["error"]


async def test_no_budget_means_no_call(toolbox: Toolbox) -> None:
    llm = FakeAnthropicClient(say("must not be called"))
    agent, _ = _agent(llm, toolbox, budget=FakeTokenBudget(daily_limit=0))

    events = await _run(agent)

    assert llm.requests == []
    assert events[-1].kind == "error"
