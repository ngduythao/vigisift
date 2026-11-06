import json
import uuid

from redis.asyncio import Redis

from vigisift.domain.device import DeviceState
from vigisift.ports.cache import PendingCommand


class RedisConfirmations:
    """One key with a TTL per request; GETDEL makes read-and-delete one atomic command."""

    def __init__(self, redis: Redis, ttl_sec: int = 300) -> None:
        self._redis = redis
        self._ttl = ttl_sec

    async def create(self, command: PendingCommand) -> str:
        confirmation_id = str(uuid.uuid4())
        await self._redis.set(
            f"confirm:{confirmation_id}",
            json.dumps({"device_id": command.device_id, "action": command.action.value}),
            ex=self._ttl,
        )
        return confirmation_id

    async def take(self, confirmation_id: str) -> PendingCommand | None:
        raw = await self._redis.getdel(f"confirm:{confirmation_id}")
        if raw is None:
            return None
        data = json.loads(raw)
        return PendingCommand(str(data["device_id"]), DeviceState(data["action"]))
