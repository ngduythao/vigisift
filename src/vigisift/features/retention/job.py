"""Delete data past its keeping time (RETENTION_DAYS, REVIEWED_RETENTION_DAYS).

Each table's own module does the delete; this job only sets the cutoffs and the pace.
Rows go in small batches, one transaction each, so a big first run (months of backlog)
never holds long locks against the live queue.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.features.alerts import repository as alerts
from vigisift.features.costs import ledger
from vigisift.features.devices import commands
from vigisift.features.telemetry import ingest
from vigisift.runtime import SessionScope

BATCH = 5000

Delete = Callable[[AsyncSession, int], Awaitable[int]]


@dataclass(frozen=True)
class Policy:
    retention_days: int
    reviewed_retention_days: int


def _deletes(policy: Policy, now: datetime) -> dict[str, Delete]:
    cutoff = now - timedelta(days=policy.retention_days)
    reviewed_cutoff = now - timedelta(days=policy.reviewed_retention_days)
    return {
        "camera_alerts": lambda db, limit: alerts.delete_expired(
            db, cutoff=cutoff, reviewed_cutoff=reviewed_cutoff, limit=limit
        ),
        "llm_calls": lambda db, limit: ledger.delete_older_than(db, cutoff, limit),
        "telemetry": lambda db, limit: ingest.delete_older_than(db, cutoff, limit),
        "commands": lambda db, limit: commands.delete_finished_before(db, cutoff, limit),
    }


async def purge(
    session: SessionScope, policy: Policy, *, now: datetime | None = None, batch: int = BATCH
) -> dict[str, int]:
    """Delete everything past its keeping time. Returns the rows deleted per table."""
    deleted: dict[str, int] = {}
    for table, delete in _deletes(policy, now or datetime.now(UTC)).items():
        total = 0
        while True:
            async with session() as db:
                gone = await delete(db, batch)
            total += gone
            if gone < batch:
                break
        deleted[table] = total
    return deleted
