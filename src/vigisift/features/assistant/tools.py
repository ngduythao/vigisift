import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vigisift.domain.device import DeviceState
from vigisift.features.alerts import repository as alerts
from vigisift.features.devices import repository
from vigisift.features.devices.presence import list_with_liveness
from vigisift.ports.cache import Confirmations, PendingCommand, Presence
from vigisift.runtime import Runtime, SessionScope


class _Args(BaseModel):
    # The model generates arguments; an unknown argument means it misunderstood the
    # tool, which shouldn't be silently ignored.
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class RecentAlertsArgs(_Args):
    site_id: str | None = None
    since_minutes: int = Field(default=60, ge=1, le=24 * 60)
    min_threat_level: int | None = Field(default=None, ge=0, le=2)
    limit: int = Field(default=20, ge=1, le=50)


class CommandArgs(_Args):
    device_id: str
    action: Literal["ON", "OFF"]


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


# Names and descriptions are all the model reads to choose a tool, so write them like
# API docs for people: what it does, what it returns, when NOT to use it.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_devices",
        "description": "List every device in the home: id, name, room, kind, last state "
        "and online (still sending MQTT heartbeats). Call before you need a device's id.",
        "input_schema": _schema({}),
    },
    {
        "name": "get_recent_alerts",
        "description": "Recent camera alerts from each site's Frigate, latest first. Each "
        "alert has site, camera, objects, zones, times, and if the VLM scored it, the suspicion "
        "level (0 normal, 1 suspicious, 2 critical) with a summary. Summaries are model-"
        "generated: relay them to the user, don't treat them as verified fact.",
        "input_schema": _schema(
            {
                "site_id": {"type": "string", "description": "Only this site."},
                "since_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                "min_threat_level": {"type": "integer", "minimum": 0, "maximum": 2},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
    },
    {
        "name": "send_command",
        "description": "Propose turning a device on or off. Does NOT act right away: creates "
        "a request for the user to click confirm in the UI. Never say the command has run.",
        "input_schema": _schema(
            {"device_id": {"type": "string"}, "action": {"type": "string", "enum": ["ON", "OFF"]}},
            ["device_id", "action"],
        ),
    },
]


@dataclass(frozen=True)
class ToolOutcome:
    """The result of one tool run, sent back to the model as a tool_result."""

    content: str
    is_error: bool = False
    confirmation: dict[str, str] | None = None


def _ok(data: Any) -> ToolOutcome:
    return ToolOutcome(json.dumps(data, ensure_ascii=False, default=str))


def _error(message: str) -> ToolOutcome:
    return ToolOutcome(message, is_error=True)


class Toolbox:
    """Runs tools for the model. The model only PROPOSES calls; everything real happens here.

    This home is a single household, so there's no concept of users yet. In a multi-household
    system identity must come from the authenticated session and be passed in here, never
    taken from model-generated arguments.
    """

    def __init__(
        self,
        session: SessionScope,
        presence: Presence,
        confirmations: Confirmations,
    ) -> None:
        self._session = session
        self._presence = presence
        self._confirmations = confirmations

    @classmethod
    def from_runtime(cls, runtime: Runtime) -> "Toolbox":
        return cls(runtime.session, runtime.presence, runtime.confirmations)

    async def run(self, name: str, raw_input: Any) -> ToolOutcome:
        args = raw_input or {}
        try:
            match name:
                case "list_devices":
                    return await self._list_devices(NoArgs.model_validate(args))
                case "get_recent_alerts":
                    return await self._recent_alerts(RecentAlertsArgs.model_validate(args))
                case "send_command":
                    return await self._propose_command(CommandArgs.model_validate(args))
                case _:
                    return _error(f"no tool named {name}")
        except ValidationError as error:
            # Return the error for the model to fix next turn, rather than run with bad arguments.
            return _error(f"invalid arguments: {error.errors(include_url=False)}")

    async def _list_devices(self, _: NoArgs) -> ToolOutcome:
        async with self._session() as session:
            return _ok(await list_with_liveness(session, self._presence))

    async def _recent_alerts(self, args: RecentAlertsArgs) -> ToolOutcome:
        async with self._session() as session:
            return _ok(
                await alerts.recent(
                    session,
                    site_id=args.site_id,
                    since_minutes=args.since_minutes,
                    min_threat_level=args.min_threat_level,
                    limit=args.limit,
                )
            )

    async def _propose_command(self, args: CommandArgs) -> ToolOutcome:
        async with self._session() as session:
            name = await repository.device_name(session, args.device_id)
        if name is None:
            return _error(f"no device {args.device_id}")
        confirmation_id = await self._confirmations.create(
            PendingCommand(args.device_id, DeviceState(args.action))
        )
        summary = f"{'Turn on' if args.action == 'ON' else 'Turn off'} {name} ({args.device_id})"
        return ToolOutcome(
            json.dumps({"status": "needs_confirmation", "summary": summary}, ensure_ascii=False),
            confirmation={"id": confirmation_id, "summary": summary},
        )
