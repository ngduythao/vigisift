import hashlib
import json
import secrets
import time

from redis.asyncio import Redis

from vigisift.ports.cache import SessionInfo


def _key(token: str) -> str:
    # Store the token's hash, not the token: anyone who reads a Valkey dump
    # still has no usable cookie.
    return f"session:{hashlib.sha256(token.encode()).hexdigest()}"


class RedisSessions:
    """Sessions with a sliding expiry: alive while active, gone after `ttl_sec` idle.

    The maximum lifetime (from sign-in) is checked by the caller through `issued_at`.
    """

    def __init__(self, redis: Redis, ttl_sec: int) -> None:
        self._redis = redis
        self._ttl = ttl_sec

    async def create(self, operator_id: int, epoch: int) -> str:
        token = secrets.token_urlsafe(32)
        value = json.dumps({"operator_id": operator_id, "epoch": epoch, "issued_at": time.time()})
        await self._redis.set(_key(token), value, ex=self._ttl)
        return token

    async def resolve(self, token: str) -> SessionInfo | None:
        raw = await self._redis.getex(_key(token), ex=self._ttl)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return SessionInfo(
                int(data["operator_id"]), int(data["epoch"]), float(data["issued_at"])
            )
        except (ValueError, KeyError, TypeError):
            # An old-format or damaged session: treat as missing, the user signs in again.
            return None

    async def revoke(self, token: str) -> None:
        await self._redis.delete(_key(token))
