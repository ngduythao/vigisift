from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.config import Settings
from vigisift.features.telemetry.hub import TelemetryHub
from vigisift.ports.cache import (
    Confirmations,
    Cooldown,
    EventBus,
    LoginAttempts,
    Presence,
    Sessions,
    TokenBudget,
)
from vigisift.ports.frigate import FrigateApi
from vigisift.ports.llm import ChatModel
from vigisift.ports.messaging import MqttClient
from vigisift.ports.rule_check import IndicatorPicker

# Open a transaction: commit when the block finishes, roll back on an error.
SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class Runtime:
    """Everything a request or an MQTT message needs, already wired.

    Holds only ports (Protocols), so features don't know whether Valkey or paho is
    behind them; bootstrap decides, and tests swap in fakes.
    """

    settings: Settings
    session: SessionScope
    mqtt: MqttClient
    hub: TelemetryHub
    events: EventBus
    presence: Presence
    cooldown: Cooldown
    budget: TokenBudget
    confirmations: Confirmations
    sessions: Sessions
    login_attempts: LoginAttempts
    frigate: FrigateApi
    chat_model: ChatModel | None
    # None when the rule re-check is off.
    rule_picker: IndicatorPicker | None = None
