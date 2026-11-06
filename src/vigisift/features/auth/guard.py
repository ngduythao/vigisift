"""Who is calling the API. Every route except sign-in goes through here."""

import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from vigisift.config import Settings
from vigisift.dependencies import RuntimeDep
from vigisift.domain.operator import Operator
from vigisift.features.auth import repository
from vigisift.runtime import Runtime


def session_cookie(settings: Settings) -> str:
    # The __Host- prefix makes browsers accept the cookie only over HTTPS, Path=/, no
    # Domain: other pages on the parent domain can't overwrite the session cookie. The
    # prefix is valid only with Secure, so the http dev build uses a plain name.
    return "__Host-vigisift_session" if settings.session_cookie_secure else "vigisift_session"


async def resolve(runtime: Runtime, token: str | None) -> Operator | None:
    """The operator of a session token, or None. An invalid session is destroyed too."""
    if not token:
        return None
    info = await runtime.sessions.resolve(token)
    if info is None:
        return None
    async with runtime.session() as session:
        found = await repository.by_id(session, info.operator_id)
    expired = time.time() - info.issued_at > runtime.settings.session_max_age_sec
    if found is None or found[1] != info.epoch or expired:
        # Account locked or deleted, password reset, or session too old.
        await runtime.sessions.revoke(token)
        return None
    return found[0]


async def require_operator(request: Request, runtime: RuntimeDep) -> Operator:
    operator = await resolve(runtime, request.cookies.get(session_cookie(runtime.settings)))
    if operator is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not_authenticated")
    return operator


async def require_admin(operator: Annotated[Operator, Depends(require_operator)]) -> Operator:
    if not operator.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin_only")
    return operator


CurrentOperator = Annotated[Operator, Depends(require_operator)]
AdminOperator = Annotated[Operator, Depends(require_admin)]
