import asyncio
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from vigisift.dependencies import WsRuntimeDep
from vigisift.features.auth.guard import resolve, session_cookie
from vigisift.features.auth.origin import same_origin

router = APIRouter(tags=["events"])


@router.websocket("/ws/events")
async def event_stream(websocket: WebSocket, runtime: WsRuntimeDep) -> None:
    settings = runtime.settings
    token = websocket.cookies.get(session_cookie(settings))
    # Check before accepting: foreign pages (even on the same parent domain, where SameSite
    # cookies are still sent) and signed-out users hear nothing.
    allowed = same_origin(
        websocket.headers.get("origin"), None, websocket.headers.get("host"), settings.public_origin
    )
    if not allowed or await resolve(runtime, token) is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    checked = time.monotonic()
    with runtime.hub.subscribe() as queue:
        try:
            while True:
                try:
                    message = await asyncio.wait_for(
                        queue.get(), timeout=settings.ws_session_check_sec
                    )
                except TimeoutError:
                    message = None
                # Signing out, being locked, or a password change must stop the event stream
                # too, not only the next HTTP request.
                if time.monotonic() - checked >= settings.ws_session_check_sec:
                    if await resolve(runtime, token) is None:
                        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                        return
                    checked = time.monotonic()
                if message is not None:
                    await websocket.send_text(message)
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
