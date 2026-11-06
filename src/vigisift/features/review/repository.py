from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.domain.operator import Verdict


async def decide(
    session: AsyncSession,
    *,
    site_id: str,
    review_id: str,
    operator_id: int,
    verdict: Verdict,
    note: str | None,
) -> dict[str, Any] | None:
    """Record (or change) an operator's verdict. None if the alert doesn't exist.

    Captures the AI's suspicion level AT decision time: a summary can arrive later, and
    knowing whether the AI led an operator on requires knowing what they saw.
    """
    row = await session.execute(
        text(
            "INSERT INTO alert_verdicts"
            " (site_id, review_id, operator_id, verdict, note, ai_threat_level)"
            " SELECT site_id, review_id, :operator, :verdict, :note,"
            " level"
            " FROM camera_alerts WHERE site_id = :site AND review_id = :review"
            " ON CONFLICT (site_id, review_id, operator_id) DO UPDATE SET"
            " verdict = EXCLUDED.verdict, note = EXCLUDED.note,"
            " ai_threat_level = EXCLUDED.ai_threat_level, decided_at = now()"
            " RETURNING site_id, review_id, verdict, decided_at"
        ),
        {
            "site": site_id,
            "review": review_id,
            "operator": operator_id,
            "verdict": verdict.value,
            "note": note,
        },
    )
    decided = row.one_or_none()
    return dict(decided._mapping) if decided is not None else None


# Only alerts in the window, each with its latest verdict (from `alert_list`) and the
# FIRST judgment by anyone (to measure handling time; changing one's mind later doesn't
# worsen this number).
_LABELLED = """
    WITH labelled AS (
        SELECT l.*, f.first_decided_at
        FROM alert_list l
        LEFT JOIN LATERAL (
            SELECT min(first_decided_at) AS first_decided_at FROM alert_verdicts
            WHERE site_id = l.site_id AND review_id = l.review_id
        ) f ON TRUE
        WHERE l.severity = 'alert' AND l.started_at > now() - make_interval(days => :days)
    )
"""


async def metrics(session: AsyncSession, *, days: int) -> list[dict[str, Any]]:
    """AI scoring against operator judgment, per site.

    "AI" is the effective level: the higher of Frigate's level and vigisift's re-check
    by camera rules, because that decides the order in the queue.

    - precision: of the alerts the AI called suspicious (level ≥ 1) that operators
      judged clearly, how many really were.
    - recall: of the alerts operators called suspicious (and the AI scored), how many
      the AI caught.
    - false_alarms_per_hour: the AI said suspicious and the operator said normal,
      divided by the window's hours. The number operators feel most.
    """
    rows = await session.execute(
        text(
            _LABELLED
            + """
            SELECT
                site_id,
                count(*) AS alerts,
                count(verdict) AS reviewed,
                count(*) FILTER (WHERE verdict = 'suspicious') AS confirmed_suspicious,
                count(*) FILTER (WHERE level >= 1) AS ai_flagged,
                count(*) FILTER (WHERE level >= 1 AND verdict = 'suspicious') AS tp,
                count(*) FILTER (WHERE level >= 1 AND verdict = 'normal') AS fp,
                count(*) FILTER (WHERE level = 0 AND verdict = 'suspicious') AS fn,
                percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY extract(epoch FROM first_decided_at - first_seen_at)
                ) FILTER (WHERE verdict IS NOT NULL) AS median_seconds_to_review
            FROM labelled GROUP BY site_id ORDER BY site_id
            """
        ),
        {"days": days},
    )
    hours = days * 24
    result = []
    for row in rows:
        tp, fp, fn = row.tp, row.fp, row.fn
        result.append(
            {
                "site_id": row.site_id,
                "alerts": row.alerts,
                "reviewed": row.reviewed,
                "confirmed_suspicious": row.confirmed_suspicious,
                "ai_flagged": row.ai_flagged,
                "precision": tp / (tp + fp) if tp + fp else None,
                "recall": tp / (tp + fn) if tp + fn else None,
                "false_alarms_per_hour": fp / hours,
                "median_seconds_to_review": row.median_seconds_to_review,
            }
        )
    return result


# Ceiling for one export: enough for a year at a mid-size center, and it stops a
# request accidentally pulling the whole table into memory.
EXPORT_LIMIT = 100_000


async def labels(session: AsyncSession, *, days: int) -> list[dict[str, Any]]:
    """Operator labels for evals: one row per judged alert, with what the AI said."""
    rows = await session.execute(
        text(
            _LABELLED
            + """
            SELECT site_id, review_id, camera, started_at, ended_at, verdict,
                   first_decided_at, level AS ai_threat_level,
                   threat_level AS frigate_threat_level, rule_level, rule_match,
                   title AS ai_title
            FROM labelled WHERE verdict IS NOT NULL ORDER BY started_at LIMIT :limit
            """
        ),
        {"days": days, "limit": EXPORT_LIMIT},
    )
    return [dict(row._mapping) for row in rows]
