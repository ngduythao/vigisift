from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict

from vigisift.dependencies import RuntimeDep, SessionDep
from vigisift.domain.device import CommandStatus, DeviceState, InvalidTransitionError
from vigisift.features.devices.commands import (
    CommandNotFoundError,
    CommandService,
    DeviceNotFoundError,
)
from vigisift.features.devices.presence import list_with_liveness

router = APIRouter(tags=["devices"])


class CommandRequest(BaseModel):
    action: DeviceState


class CommandResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    command_id: str
    device_id: str
    action: DeviceState
    duplicate: bool
    status: CommandStatus


async def submit_command(
    service: CommandService, device_id: str, action: DeviceState, idempotency_key: str
) -> CommandResponse:
    """Send a command, translating domain errors to HTTP. Shared with the assistant's confirm."""
    try:
        command = await service.send(device_id, action, idempotency_key)
    except DeviceNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="device_not_found") from error
    except InvalidTransitionError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"cannot_go_from_{error.current.lower()}_to_{error.target.lower()}",
        ) from error
    return CommandResponse.model_validate(command)


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
