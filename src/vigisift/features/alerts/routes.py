import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from vigisift.dependencies import RuntimeDep, SessionDep
from vigisift.features.alerts import repository
from vigisift.features.alerts.view import AlertView
from vigisift.ports.frigate import FrigateClipMissingError, FrigateUnavailableError

log = logging.getLogger(__name__)

router = APIRouter(tags=["alerts"])


class SiteView(BaseModel):
    """No `api_url`: that's the internal address the server uses, not the operator's."""

    id: str
    name: str
    ui_url: str


@router.get("/sites")
async def sites(session: SessionDep) -> list[SiteView]:
    return [SiteView(**site) for site in await repository.list_sites(session)]


@router.get("/alerts")
async def alerts(
    session: SessionDep,
    site_id: str | None = None,
    min_threat_level: Annotated[int | None, Query(ge=0, le=2)] = None,
    since_minutes: Annotated[int, Query(ge=1, le=7 * 24 * 60)] = 24 * 60,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    camera: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9_-]{1,64}$")] = None,
    started_after: datetime | None = None,
    started_before: datetime | None = None,
) -> list[AlertView]:
    """Latest alerts first.

    `min_threat_level` compares with the effective level and matches only summarised
    alerts: an alert not scored yet (or never to be) has no level to compare.
    """
    rows = await repository.recent(
        session,
        site_id=site_id,
        since_minutes=since_minutes,
        min_threat_level=min_threat_level,
        limit=limit,
        camera=camera,
        started_after=started_after,
        started_before=started_before,
    )
    return [AlertView(**row) for row in rows]


async def _rest_of_clip(first: bytes, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    yield first
    try:
        async for chunk in chunks:
            yield chunk
    except FrigateUnavailableError as error:
        # The status line has gone out, so the player just sees the video stop.
        log.warning("clip cut off: %s", error)


@router.get("/alerts/{site_id}/{review_id}/clip")
async def clip(site_id: str, review_id: str, runtime: RuntimeDep) -> StreamingResponse:
    """The alert's video, fetched from its site's Frigate.

    Operators watch it here without a Frigate account, and only for alerts VigiSift
    knows. Not SessionDep: a database connection must not stay checked out while a
    video plays.
    """
    async with runtime.session() as session:
        site = await repository.clip_source(session, site_id, review_id)
    if site is None:
        raise HTTPException(status_code=404, detail="alert_not_found")
    chunks = runtime.frigate.review_clip(site, review_id)
    try:
        first = await anext(chunks)
    except (FrigateClipMissingError, StopAsyncIteration) as error:
        raise HTTPException(status_code=404, detail="clip_not_available") from error
    except FrigateUnavailableError as error:
        log.warning("clip unavailable: %s", error)
        raise HTTPException(status_code=503, detail="frigate_unavailable") from error
    return StreamingResponse(
        _rest_of_clip(first, chunks),
        media_type="video/mp4",
        # CCTV footage must not stay in a shared workstation's browser cache.
        headers={"Cache-Control": "no-store"},
    )
