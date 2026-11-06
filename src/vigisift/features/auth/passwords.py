import asyncio
import weakref

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# argon2id with the library's default parameters (the RFC 9106 low-memory profile).
_HASHER = PasswordHasher()
# Hash of a random string, so a username that doesn't exist also costs exactly
# one verify: response time can't reveal which names are real.
_DUMMY = _HASHER.hash("dummy-password-never-matches")
# argon2 is deliberately CPU-heavy (~25 ms) and synchronous: run in a thread so it doesn't
# block the event loop (all other requests and WebSockets), and limit concurrent runs
# so a burst of sign-ins can't eat all the CPU. One semaphore per event loop: a
# shared semaphore would bind to the first loop that waits on it.
_LIMITS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _limit() -> asyncio.Semaphore:
    return _LIMITS.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(4))


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def _verify(password_hash: str | None, password: str) -> bool:
    try:
        return _HASHER.verify(password_hash or _DUMMY, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def verify_password(password_hash: str | None, password: str) -> bool:
    async with _limit():
        return await asyncio.to_thread(_verify, password_hash, password)
