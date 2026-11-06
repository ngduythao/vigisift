from enum import StrEnum


class DeviceState(StrEnum):
    OFFLINE = "OFFLINE"
    ONLINE = "ONLINE"
    ON = "ON"
    OFF = "OFF"


# The table of valid transitions lives in one place, and the service asks it before
# writing to the DB.
ALLOWED: dict[DeviceState, frozenset[DeviceState]] = {
    DeviceState.OFFLINE: frozenset({DeviceState.ONLINE}),
    DeviceState.ONLINE: frozenset({DeviceState.ON, DeviceState.OFF, DeviceState.OFFLINE}),
    DeviceState.ON: frozenset({DeviceState.OFF, DeviceState.OFFLINE}),
    DeviceState.OFF: frozenset({DeviceState.ON, DeviceState.OFFLINE}),
}


class InvalidTransitionError(Exception):
    def __init__(self, current: DeviceState, target: DeviceState) -> None:
        super().__init__(f"{current} -> {target}")
        self.current = current
        self.target = target


def can_transition(current: DeviceState, target: DeviceState) -> bool:
    return target in ALLOWED.get(current, frozenset())


def validate_transition(current: DeviceState, target: DeviceState) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)


class CommandStatus(StrEnum):
    """A command's lifecycle. Only PENDING may change; the other three are final."""

    PENDING = "PENDING"
    ACKED = "ACKED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
