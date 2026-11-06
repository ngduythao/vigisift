from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from vigisift.dependencies import RuntimeDep, SessionDep
from vigisift.domain.operator import Role
from vigisift.features.auth.guard import CurrentOperator, session_cookie
from vigisift.features.auth.service import (
    InvalidCredentialsError,
    LoginPolicy,
    TooManyAttemptsError,
    authenticate,
)

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class OperatorView(BaseModel):
    id: int
    username: str
    display_name: str
    role: Role


@router.post("/auth/login", status_code=status.HTTP_204_NO_CONTENT)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    runtime: RuntimeDep,
    session: SessionDep,
) -> None:
    settings = runtime.settings
    try:
        operator, epoch = await authenticate(
            session,
            runtime.login_attempts,
            LoginPolicy(settings.login_max_failures, settings.login_window_sec),
            payload.username,
            payload.password,
            request.client.host if request.client else "unknown",
        )
    except TooManyAttemptsError as error:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, detail="too_many_attempts"
        ) from error
    except InvalidCredentialsError as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials") from error
    token = await runtime.sessions.create(operator.id, epoch)
    response.set_cookie(
        session_cookie(settings),
        token,
        max_age=settings.session_ttl_sec,
        httponly=True,
        # Strict: browsers don't send the cookie on requests started from another
        # site, so a foreign page can't judge alerts on an operator's behalf (CSRF).
        samesite="strict",
        secure=settings.session_cookie_secure,
        path="/",
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, runtime: RuntimeDep) -> None:
    name = session_cookie(runtime.settings)
    token = request.cookies.get(name)
    if token:
        await runtime.sessions.revoke(token)
    response.delete_cookie(name, path="/", secure=runtime.settings.session_cookie_secure)


@router.get("/auth/me")
async def me(operator: CurrentOperator) -> OperatorView:
    return OperatorView(
        id=operator.id,
        username=operator.username,
        display_name=operator.display_name,
        role=operator.role,
    )
