from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def get_ws_runtime(websocket: WebSocket) -> Runtime:
    runtime: Runtime = websocket.app.state.runtime
    return runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
WsRuntimeDep = Annotated[Runtime, Depends(get_ws_runtime)]


async def get_session(runtime: RuntimeDep) -> AsyncIterator[AsyncSession]:
    async with runtime.session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
