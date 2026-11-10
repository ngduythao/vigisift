import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import Row, TextClause, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.domain.device import CommandStatus, DeviceState, validate_transition
from vigisift.domain.events import EventType
from vigisift.features.devices import repository
from vigisift.features.topics import command_topic
from vigisift.ports.messaging import MqttClient


class DeviceNotFoundError(Exception):
    pass


class CommandNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class Command:
    command_id: str
    device_id: str
    action: DeviceState
    status: CommandStatus
    duplicate: bool = False

    def event(self) -> dict[str, object]:
        return {
            "type": EventType.COMMAND,
            "command_id": self.command_id,
            "device_id": self.device_id,
            "status": self.status,
        }


class Ack(BaseModel):
    """The device reports it carried out a command (or didn't), on `device/<id>/ack`."""

    model_config = ConfigDict(frozen=True)

    command_id: str = Field(min_length=1)
    ok: StrictBool


_COLUMNS = "id, device_id, action, status"
_BY_KEY = text(f"SELECT {_COLUMNS} FROM commands WHERE idempotency_key = :key")
_BY_ID = text(f"SELECT {_COLUMNS} FROM commands WHERE id = :id")


class CommandService:
    """A command's lifecycle: send it (QoS 1), settle on ack, expire on silence."""

    def __init__(self, session: AsyncSession, mqtt: MqttClient) -> None:
        self._session = session
        self._mqtt = mqtt

    async def send(self, device_id: str, action: DeviceState, idempotency_key: str) -> Command:
        existing = await self._find(_BY_KEY, {"key": idempotency_key})
        if existing is not None:
            return existing

        state = await repository.lock_state(self._session, device_id)
        if state is None:
            raise DeviceNotFoundError(device_id)
        validate_transition(state, action)

        command_id = str(uuid4())
        try:
            await self._session.execute(
                text(
                    "INSERT INTO commands (id, device_id, idempotency_key, action, created_at)"
                    " VALUES (:id, :device, :key, :action, now())"
                ),
                {"id": command_id, "device": device_id, "key": idempotency_key, "action": action},
            )
            await self._session.flush()
        except IntegrityError:
            # Two requests with the same key at the same moment: the DB's UNIQUE constraint
            # is the referee, and the loser reads back the winner's record.
            await self._session.rollback()
            winner = await self._find(_BY_KEY, {"key": idempotency_key})
            if winner is None:
                raise
            return winner

        # QoS 1: the broker holds and redelivers until the device gets it. In exchange it
        # can deliver twice, so the payload carries command_id for the device to skip the
        # second. QoS 2 (exactly once) costs more and still says nothing about whether
        # the device CARRIED OUT the command; that's the ack's job.
        self._mqtt.publish(
            command_topic(device_id),
            json.dumps({"command_id": command_id, "action": action.value}),
            qos=1,
        )
        return Command(command_id, device_id, action, CommandStatus.PENDING)

    async def get(self, command_id: str) -> Command:
        found = await self._find(_BY_ID, {"id": command_id}, duplicate=False)
        if found is None:
            raise CommandNotFoundError(command_id)
        return found

    async def resolve(self, device_id: str, ack: Ack) -> Command | None:
        """Settle a command by ack. Only a command still PENDING, of that same device.

        QoS 1 can deliver an ack twice; the `status = 'PENDING'` condition makes the
        second a no-op. The `device_id` condition stops one device acking another
        device's command.
        """
        rows = await self._session.execute(
            text(
                "UPDATE commands SET status = :status, resolved_at = now()"
                " WHERE id = :id AND device_id = :device AND status = 'PENDING'"
                f" RETURNING {_COLUMNS}"
            ),
            {
                "status": CommandStatus.ACKED if ack.ok else CommandStatus.FAILED,
                "id": ack.command_id,
                "device": device_id,
            },
        )
        return next((_command(row) for row in rows), None)

    async def expire(self, timeout_sec: int) -> list[Command]:
        """Mark overdue commands with no ack as TIMED_OUT.

        Every instance runs this sweep; the conditional UPDATE means each command changes
        state exactly once even if two instances sweep together.
        """
        rows = await self._session.execute(
            text(
                "UPDATE commands SET status = 'TIMED_OUT', resolved_at = now()"
                " WHERE status = 'PENDING'"
                " AND created_at < now() - make_interval(secs => :timeout)"
                f" RETURNING {_COLUMNS}"
            ),
            {"timeout": timeout_sec},
        )
        return [_command(row) for row in rows]

    async def _find(
        self, query: TextClause, params: dict[str, str], duplicate: bool = True
    ) -> Command | None:
        rows = await self._session.execute(query, params)
        return next((_command(row, duplicate) for row in rows), None)


def _command(row: Row[Any], duplicate: bool = False) -> Command:
    return Command(
        str(row.id),
        str(row.device_id),
        DeviceState(row.action),
        CommandStatus(row.status),
        duplicate,
    )
