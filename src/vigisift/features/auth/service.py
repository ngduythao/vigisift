from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.domain.operator import Operator
from vigisift.features.auth import repository
from vigisift.features.auth.passwords import verify_password
from vigisift.ports.cache import LoginAttempts


class TooManyAttemptsError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


@dataclass(frozen=True)
class LoginPolicy:
    # Attempts for one (IP, username) pair: stops password guessing from one machine.
    max_failures: int
    window_sec: int
    # A shared ceiling per name (distributed guessing) and per IP (one machine
    # trying many names). Much higher than the per-pair ceiling, so a stranger can't
    # lock out an on-duty operator's account with a few wrong entries.
    user_ceiling_factor: int = 10
    ip_ceiling_factor: int = 20


async def authenticate(
    session: AsyncSession,
    attempts: LoginAttempts,
    policy: LoginPolicy,
    username: str,
    password: str,
    client_ip: str,
) -> tuple[Operator, int]:
    """Check the password, with guessing protection. Returns the operator and session epoch.

    Each attempt is COUNTED BEFORE checking the password, by an atomic increment:
    with 100 concurrent requests only `max_failures` get checked, and the rest
    are refused. While blocked even the right password is refused, or a guesser would
    still know when they hit it. The returned error doesn't say which names are real.
    """
    user = username.lower()
    pair, user_key, ip_key = f"pair:{client_ip}|{user}", f"user:{user}", f"ip:{client_ip}"
    counts = [await attempts.hit(key, policy.window_sec) for key in (pair, user_key, ip_key)]
    limits = (
        policy.max_failures,
        policy.max_failures * policy.user_ceiling_factor,
        policy.max_failures * policy.ip_ceiling_factor,
    )
    if any(count > limit for count, limit in zip(counts, limits, strict=True)):
        raise TooManyAttemptsError
    found = await repository.credentials(session, user)
    if not await verify_password(found[1] if found else None, password) or found is None:
        raise InvalidCredentialsError
    await attempts.reset(pair)
    await attempts.release(user_key)
    await attempts.release(ip_key)
    operator, _, epoch = found
    return operator, epoch
