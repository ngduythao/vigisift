from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from vigisift.dependencies import RuntimeDep, SessionDep
from vigisift.domain.usage import LlmUsage
from vigisift.features.assistant.agent import Agent, AgentEvent
from vigisift.features.assistant.tools import Toolbox
from vigisift.features.costs.ledger import record_call
from vigisift.features.devices.commands import CommandService
from vigisift.features.devices.routes import CommandResponse, submit_command
from vigisift.ports.llm import ChatUnavailableError
from vigisift.runtime import Runtime

router = APIRouter(tags=["assistant"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


def build_agent(runtime: Runtime) -> Agent:
    if runtime.chat_model is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="assistant_disabled")

    async def on_usage(usage: LlmUsage) -> None:
        async with runtime.session() as session:
            await record_call(session, "assistant", usage)

    return Agent(runtime.chat_model, Toolbox.from_runtime(runtime), runtime.budget, on_usage)


@router.post("/assistant/chat")
async def chat(payload: ChatRequest, runtime: RuntimeDep) -> StreamingResponse:
    """Stream the answer with Server-Sent Events: text appears as it arrives."""
    agent = build_agent(runtime)

    async def events() -> AsyncIterator[str]:
        try:
            async for event in agent.run(payload.message):
                yield event.sse()
        except ChatUnavailableError:
            # Don't push provider error details to the client.
            yield AgentEvent("error", {"message": "assistant_unavailable"}).sse()

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/assistant/confirmations/{confirmation_id}", response_model=CommandResponse)
async def confirm(
    confirmation_id: str, runtime: RuntimeDep, session: SessionDep
) -> CommandResponse:
    """The user clicked confirm: only now is the command actually sent to the device."""
    pending = await runtime.confirmations.take(confirmation_id)
    if pending is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="confirmation_not_found_or_expired")
    return await submit_command(
        CommandService(session, runtime.mqtt),
        pending.device_id,
        pending.action,
        f"assistant-{confirmation_id}",
    )
