import json
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from vigisift.dependencies import RuntimeDep, SessionDep
from vigisift.domain.events import EventType
from vigisift.domain.operator import Verdict
from vigisift.features.alerts import repository as alerts
from vigisift.features.alerts.view import AlertView
from vigisift.features.auth.guard import AdminOperator, CurrentOperator
from vigisift.features.review import repository

router = APIRouter(tags=["review"])


class VerdictRequest(BaseModel):
    verdict: Verdict
    note: str | None = Field(default=None, max_length=500)


class VerdictView(BaseModel):
    site_id: str
    review_id: str
    verdict: Verdict
    decided_at: datetime


class SiteMetrics(BaseModel):
    site_id: str
    alerts: int
    reviewed: int
    confirmed_suspicious: int
    ai_flagged: int
    precision: float | None
    recall: float | None
    false_alarms_per_hour: float
    median_seconds_to_review: float | None


@router.get("/queue")
async def review_queue(
    session: SessionDep,
    site_id: str | None = None,
    since_minutes: Annotated[int, Query(ge=1, le=7 * 24 * 60)] = 7 * 24 * 60,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AlertView]:
    rows = await alerts.unjudged(session, site_id=site_id, since_minutes=since_minutes, limit=limit)
    return [AlertView(**row) for row in rows]


@router.put("/alerts/{site_id}/{review_id}/verdict")
async def decide(
    site_id: str,
    review_id: str,
    payload: VerdictRequest,
    operator: CurrentOperator,
    runtime: RuntimeDep,
    session: SessionDep,
) -> VerdictView:
    """Who judged comes from the session, never from the request body."""
    note = payload.note.strip() if payload.note and payload.note.strip() else None
    decided = await repository.decide(
        session,
        site_id=site_id,
        review_id=review_id,
        operator_id=operator.id,
        verdict=payload.verdict,
        note=note,
    )
    if decided is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="alert_not_found")
    await session.commit()
    # Other operators see the alert leave their queue right away.
    await runtime.events.publish(
        {
            "type": EventType.ALERT_VERDICT,
            "site_id": site_id,
            "review_id": review_id,
            "verdict": payload.verdict.value,
            "by": operator.username,
        }
    )
    return VerdictView(**decided)


@router.get("/review-metrics")
async def review_metrics(
    session: SessionDep, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> list[SiteMetrics]:
    return [SiteMetrics(**row) for row in await repository.metrics(session, days=days)]


@router.get("/verdicts/export")
async def export_labels(
    _admin: AdminOperator, session: SessionDep, days: Annotated[int, Query(ge=1, le=365)] = 30
) -> StreamingResponse:
    """Operator labels as JSON Lines, input for the eval set. Admin only."""
    rows = await repository.labels(session, days=days)

    def lines() -> Iterator[str]:
        for row in rows:
            yield json.dumps(row, default=_json_default, ensure_ascii=False) + "\n"

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="verdicts.jsonl"'},
    )


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)
