from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.domain.usage import LlmUsage, cost_usd


async def record_call(session: AsyncSession, purpose: str, usage: LlmUsage) -> Decimal:
    cost = cost_usd(usage)
    await session.execute(
        text(
            "INSERT INTO llm_calls (purpose, model, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, cost_usd)"
            " VALUES (:purpose, :model, :input, :output, :read, :write, :cost)"
        ),
        {
            "purpose": purpose,
            "model": usage.model,
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "read": usage.cache_read_tokens,
            "write": usage.cache_write_tokens,
            "cost": cost,
        },
    )
    return cost


async def delete_older_than(session: AsyncSession, cutoff: datetime, limit: int) -> int:
    result = await session.execute(
        text(
            "DELETE FROM llm_calls WHERE id IN"
            " (SELECT id FROM llm_calls WHERE created_at < :cutoff LIMIT :limit)"
        ),
        {"cutoff": cutoff, "limit": limit},
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
