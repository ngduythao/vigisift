"""Sending a command over HTTP, shared by the devices API and the assistant's confirm."""

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict

from vigisift.domain.device import CommandStatus, DeviceState, InvalidTransitionError
from vigisift.features.devices.commands import CommandService, DeviceNotFoundError


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
    """Send a command, translating domain errors to HTTP."""
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
