from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from vigisift.domain.device import DeviceState


class Presence(Protocol):
    """Who is still sending heartbeats.

    Last Will only catches dead TCP connections. If device software hangs while the
    socket stays open the broker doesn't know; only the heartbeat stopping reveals it.
    Heartbeats go to Redis, not Postgres: each telemetry message would be a write, and
    this data can be lost.
    """

    async def touch(self, device_id: str) -> None: ...

    async def forget(self, device_id: str) -> None: ...

    async def online(self) -> set[str]: ...


class Cooldown(Protocol):
    """Claim the right to do something at most once every `seconds`, across all instances.

    For costly work, or work only one instance should do each cycle (reconcile).
    """

    async def acquire(self, key: str, seconds: int) -> bool: ...


class TokenBudget(Protocol):
    """Daily LLM token ceiling. Past it, calls are skipped, not data."""

    async def remaining(self) -> int: ...

    async def spend(self, tokens: int) -> None: ...


@dataclass(frozen=True)
class PendingCommand:
    device_id: str
    action: DeviceState


class Confirmations(Protocol):
    """A command the agent proposed, waiting for a person to confirm. Expires by itself."""

    async def create(self, command: PendingCommand) -> str: ...

    async def take(self, confirmation_id: str) -> PendingCommand | None:
        """Read and delete in one step: clicking twice runs it once."""
        ...


class EventBus(Protocol):
    """Push realtime events to every browser, whichever instance it's connected to."""

    async def publish(self, event: Mapping[str, object]) -> None: ...


@dataclass(frozen=True)
class SessionInfo:
    operator_id: int
    # The account's password generation when the session was created. Resetting a
    # password bumps it, so all older sessions (a stolen cookie's too) die with it.
    epoch: int
    issued_at: float


class Sessions(Protocol):
    """Server-side sessions: a random token in the cookie, the data in the cache.

    Signing out or locking an account takes effect at once, unlike a JWT waiting to expire.
    """

    async def create(self, operator_id: int, epoch: int) -> str:
        """Return the token for the cookie."""
        ...

    async def resolve(self, token: str) -> SessionInfo | None:
        """The token's session, extending its idle time. None if missing or expired."""
        ...

    async def revoke(self, token: str) -> None: ...


class LoginAttempts(Protocol):
    """Count sign-in attempts per key within a time window."""

    async def hit(self, key: str, window_sec: int) -> int:
        """Increment and return the new count in ONE atomic command: concurrent
        requests can't all read the old number."""
        ...

    async def release(self, key: str) -> None:
        """Give one attempt back (a successful sign-in doesn't count toward the shared ceiling)."""
        ...

    async def reset(self, key: str) -> None: ...
