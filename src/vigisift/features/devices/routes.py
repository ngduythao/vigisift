from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from vigisift.dependencies import RuntimeDep, SessionDep
from vigisift.domain.device import DeviceState
from vigisift.features.devices.commands import CommandNotFoundError, CommandService
from vigisift.features.devices.http import CommandResponse, submit_command
from vigisift.features.devices.presence import list_with_liveness

router = APIRouter(tags=["devices"])


class CommandRequest(BaseModel):
    action: DeviceState


@router.get("/devices")
async def list_devices(runtime: RuntimeDep, session: SessionDep) -> list[dict[str, Any]]:
    return await list_with_liveness(session, runtime.presence)


@router.post("/devices/{device_id}/commands", response_model=CommandResponse)
async def send_command(
    device_id: str,
    payload: CommandRequest,
    runtime: RuntimeDep,
    session: SessionDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> CommandResponse:
    service = CommandService(session, runtime.mqtt)
    return await submit_command(service, device_id, payload.action, idempotency_key)


@router.get("/commands/{command_id}", response_model=CommandResponse)
async def get_command(command_id: str, runtime: RuntimeDep, session: SessionDep) -> CommandResponse:
    try:
        command = await CommandService(session, runtime.mqtt).get(command_id)
    except CommandNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="command_not_found") from error
    return CommandResponse.model_validate(command)
