from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text

from vigisift.dependencies import SessionDep

router = APIRouter(tags=["llm"])


class UsageLine(BaseModel):
    purpose: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float
    cost_per_100_calls_usd: float


@router.get("/llm/usage")
async def usage(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=90)] = 1,
) -> list[UsageLine]:
    """LLM cost by purpose and model. Answers "what do 100 events cost"."""
    rows = await session.execute(
        text(
            "SELECT purpose, model, count(*) AS calls, sum(input_tokens) AS input,"
            " sum(output_tokens) AS output, sum(cache_read_tokens) AS cache_read,"
            " sum(cost_usd) AS cost"
            " FROM llm_calls WHERE created_at > now() - make_interval(days => :days)"
            " GROUP BY purpose, model ORDER BY cost DESC"
        ),
        {"days": days},
    )
    return [
        UsageLine(
            purpose=row.purpose,
            model=row.model,
            calls=row.calls,
            input_tokens=row.input,
            output_tokens=row.output,
            cache_read_tokens=row.cache_read,
            cost_usd=float(row.cost),
            cost_per_100_calls_usd=float(row.cost) * 100 / row.calls,
        )
        for row in rows
    ]
