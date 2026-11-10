from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from vigisift.domain.alert import AlertUpdate, review_link
from vigisift.ports.frigate import FrigateSite

# An alert as operators see it: the `alert_list` view in schema.sql.
_READ = text("SELECT * FROM alert_list WHERE site_id = :site AND review_id = :review")

# Lock the row so merges and re-checks of one review queue up.
_LOCK = text("SELECT 1 FROM camera_alerts WHERE site_id = :site AND review_id = :review FOR UPDATE")

_COLUMNS_AND_VALUES = """
    INSERT INTO camera_alerts (
        site_id, review_id, camera, severity, objects, zones, started_at, ended_at,
        times_approximate, threat_level, title, summary, scene, confidence, other_concerns,
        observations, summarised_at
    ) VALUES (
        :site, :review, :camera, :severity, :objects, :zones, :started_at, :ended_at,
        :approximate, :threat_level, :title, :summary, :scene, :confidence, :other_concerns,
        :observations, CASE WHEN CAST(:threat_level AS SMALLINT) IS NULL THEN NULL ELSE now() END
    )
"""

# First sighting of a review. Exactly ONE transaction wins this INSERT, even
# when three duplicates arrive together: the rest wait at the unique index and
# get back 0 rows. So a "new alert" reaches the screen only once.
_INSERT_NEW = text(_COLUMNS_AND_VALUES + " ON CONFLICT (site_id, review_id) DO NOTHING RETURNING 1")

# Every merge is commutative and idempotent (a poor man's CRDT):
# Frigate reports new → update → end → genai, but through MQTT shared subscriptions
# and reconcile passes, reports can arrive out of order or duplicated. Whatever the
# order, the final row must be the same.
#
# In SET, `camera_alerts.x` is the OLD value, `EXCLUDED.x` the newly arrived one.
_MERGE = text(
    _COLUMNS_AND_VALUES
    + """
    ON CONFLICT (site_id, review_id) DO UPDATE SET
        camera = EXCLUDED.camera,
        -- Frigate only raises detection to alert, never lowers it.
        severity = CASE WHEN 'alert' IN (camera_alerts.severity, EXCLUDED.severity)
                        THEN 'alert' ELSE 'detection' END,
        objects = ARRAY(SELECT DISTINCT x FROM unnest(camera_alerts.objects || EXCLUDED.objects) x
                        ORDER BY x),
        zones = ARRAY(SELECT DISTINCT x FROM unnest(camera_alerts.zones || EXCLUDED.zones) x
                      ORDER BY x),
        -- Exact times beat approximate ones (widened by Frigate);
        -- of the same kind, take the widest span. GREATEST ignores NULL.
        started_at = CASE
            WHEN camera_alerts.times_approximate = EXCLUDED.times_approximate
                THEN LEAST(camera_alerts.started_at, EXCLUDED.started_at)
            WHEN camera_alerts.times_approximate THEN EXCLUDED.started_at
            ELSE camera_alerts.started_at END,
        ended_at = CASE
            WHEN camera_alerts.times_approximate = EXCLUDED.times_approximate
                THEN GREATEST(camera_alerts.ended_at, EXCLUDED.ended_at)
            WHEN camera_alerts.times_approximate THEN EXCLUDED.ended_at
            ELSE camera_alerts.ended_at END,
        times_approximate = camera_alerts.times_approximate AND EXCLUDED.times_approximate,
        -- A summary travels whole: don't mix this report's title with another's scene.
        threat_level = COALESCE(EXCLUDED.threat_level, camera_alerts.threat_level),
        title = CASE WHEN EXCLUDED.threat_level IS NULL THEN camera_alerts.title
                     ELSE EXCLUDED.title END,
        summary = CASE WHEN EXCLUDED.threat_level IS NULL THEN camera_alerts.summary
                       ELSE EXCLUDED.summary END,
        scene = CASE WHEN EXCLUDED.threat_level IS NULL THEN camera_alerts.scene
                     ELSE EXCLUDED.scene END,
        confidence = CASE WHEN EXCLUDED.threat_level IS NULL THEN camera_alerts.confidence
                          ELSE EXCLUDED.confidence END,
        other_concerns = CASE WHEN EXCLUDED.threat_level IS NULL THEN camera_alerts.other_concerns
                              ELSE EXCLUDED.other_concerns END,
        observations = CASE WHEN EXCLUDED.threat_level IS NULL THEN camera_alerts.observations
                            ELSE EXCLUDED.observations END,
        -- A DIFFERENT summary (Frigate summarised again) voids the old re-check
        -- result: clear it so the re-check loop runs again. The same summary keeps it.
        rule_checked_at = CASE WHEN EXCLUDED.threat_level IS NOT NULL
            AND (EXCLUDED.summary, EXCLUDED.scene) IS DISTINCT FROM
                (camera_alerts.summary, camera_alerts.scene)
            THEN NULL ELSE camera_alerts.rule_checked_at END,
        rule_level = CASE WHEN EXCLUDED.threat_level IS NOT NULL
            AND (EXCLUDED.summary, EXCLUDED.scene) IS DISTINCT FROM
                (camera_alerts.summary, camera_alerts.scene)
            THEN NULL ELSE camera_alerts.rule_level END,
        rule_match = CASE WHEN EXCLUDED.threat_level IS NOT NULL
            AND (EXCLUDED.summary, EXCLUDED.scene) IS DISTINCT FROM
                (camera_alerts.summary, camera_alerts.scene)
            THEN NULL ELSE camera_alerts.rule_match END,
        summarised_at = COALESCE(camera_alerts.summarised_at, EXCLUDED.summarised_at),
        updated_at = now()
    """
)


class UnknownSiteError(Exception):
    pass


def _params(update: AlertUpdate) -> dict[str, Any]:
    summary = update.summary
    return {
        "site": update.site_id,
        "review": update.review_id,
        "camera": update.camera,
        "severity": update.severity.value,
        "objects": list(update.objects),
        "zones": list(update.zones),
        "started_at": update.started_at,
        "ended_at": update.ended_at,
        "approximate": update.times_approximate,
        "threat_level": int(summary.threat_level) if summary else None,
        "title": summary.title if summary else None,
        "summary": summary.summary if summary else None,
        "scene": summary.scene if summary else None,
        "confidence": summary.confidence if summary else None,
        "other_concerns": list(summary.other_concerns) if summary else None,
        "observations": list(summary.observations) if summary else None,
    }


def alert_row(row: Any) -> dict[str, Any]:
    """A row of `alert_list` as the API and live events show it."""
    alert = dict(row._mapping)
    alert["link"] = review_link(alert.pop("ui_url"), alert["review_id"])
    return alert


async def _read(session: AsyncSession, key: dict[str, str]) -> Any:
    return (await session.execute(_READ, key)).one()


def _same(before: Any, after: Any) -> bool:
    """Whether a merge changed what operators see. `updated_at` doesn't count: every
    merge stamps it, even one that changes nothing."""
    old, new = dict(before._mapping), dict(after._mapping)
    old.pop("updated_at")
    new.pop("updated_at")
    return old == new


async def record(session: AsyncSession, update: AlertUpdate) -> dict[str, Any] | None:
    """Merge one report into the alert. Returns the alert if it changed, otherwise None.

    An unknown site is refused rather than created, like an unknown device in telemetry: the
    ACL already blocks spoofed sites, so an unknown site can only be an unregistered one.
    """
    known = await session.execute(
        text("SELECT 1 FROM sites WHERE id = :id"), {"id": update.site_id}
    )
    if known.scalar_one_or_none() is None:
        raise UnknownSiteError(update.site_id)
    key = {"site": update.site_id, "review": update.review_id}
    params = _params(update)
    if (await session.execute(_INSERT_NEW, params)).one_or_none() is not None:
        return alert_row(await _read(session, key))
    # The row exists: lock it so merges of this review queue up, then compare before/after
    # to push an event only when something is really new.
    await session.execute(_LOCK, key)
    before = await _read(session, key)
    await session.execute(_MERGE, params)
    after = await _read(session, key)
    return None if _same(before, after) else alert_row(after)


async def recent(
    session: AsyncSession,
    *,
    site_id: str | None,
    since_minutes: int,
    min_threat_level: int | None,
    limit: int,
    camera: str | None = None,
    started_after: datetime | None = None,
    started_before: datetime | None = None,
) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT * FROM alert_list"
            " WHERE started_at > now() - make_interval(mins => :since)"
            " AND (CAST(:site AS TEXT) IS NULL OR site_id = :site)"
            " AND (CAST(:camera AS TEXT) IS NULL OR camera = :camera)"
            " AND (CAST(:after AS TIMESTAMPTZ) IS NULL OR started_at >= :after)"
            " AND (CAST(:before AS TIMESTAMPTZ) IS NULL OR started_at < :before)"
            " AND (CAST(:min_threat AS SMALLINT) IS NULL OR level >= :min_threat)"
            " ORDER BY started_at DESC LIMIT :limit"
        ),
        {
            "since": since_minutes,
            "site": site_id,
            "camera": camera,
            "after": started_after,
            "before": started_before,
            "min_threat": min_threat_level,
            "limit": limit,
        },
    )
    return [alert_row(row) for row in rows]


async def unjudged(
    session: AsyncSession, *, site_id: str | None, since_minutes: int, limit: int
) -> list[dict[str, Any]]:
    """Alerts (severity alert) nobody has judged, for the review queue.

    Most suspicious first. Alerts without a summary sit between "suspicious" and
    "normal": unknown means it needs a look, but mustn't hide alerts the AI found
    suspicious. Within a level, latest first.
    """
    rows = await session.execute(
        text(
            "SELECT * FROM alert_list"
            " WHERE severity = 'alert' AND verdict IS NULL"
            " AND started_at > now() - make_interval(mins => :since)"
            " AND (CAST(:site AS TEXT) IS NULL OR site_id = :site)"
            " ORDER BY COALESCE(level, 0.5) DESC, started_at DESC LIMIT :limit"
        ),
        {"since": since_minutes, "site": site_id, "limit": limit},
    )
    return [alert_row(row) for row in rows]


# The rule re-check queue (features/rules/check.py): summarised alerts not checked yet, on
# cameras that have rules. One transaction per alert: SKIP LOCKED lets instances share
# the queue without duplicate checks. Only summaries within a day: adding rules for a
# camera today doesn't drag in a re-check of its whole history.
_NEXT_TO_CHECK = text(
    "SELECT a.site_id, a.review_id, a.title, a.summary, a.scene, a.observations,"
    " r.rules, r.sha"
    " FROM camera_alerts a"
    " JOIN camera_rules r ON r.site_id = a.site_id AND r.camera = a.camera"
    " WHERE a.rule_checked_at IS NULL AND a.summarised_at IS NOT NULL"
    " AND a.summarised_at > now() - interval '1 day'"
    " ORDER BY a.summarised_at LIMIT 1 FOR UPDATE OF a SKIP LOCKED"
)

_SAVE_CHECK = text(
    "UPDATE camera_alerts SET rule_level = :level, rule_match = :match, rule_sha = :sha,"
    " rule_checked_at = now() WHERE site_id = :site AND review_id = :review"
)


async def next_to_check(session: AsyncSession) -> Any | None:
    """The next alert waiting for a rule re-check, locked, with its camera's rules."""
    return (await session.execute(_NEXT_TO_CHECK)).one_or_none()


async def save_check(
    session: AsyncSession,
    site_id: str,
    review_id: str,
    *,
    level: int,
    match: str | None,
    sha: str,
) -> dict[str, Any]:
    """Store a re-check result and return the alert as operators now see it."""
    key = {"site": site_id, "review": review_id}
    await session.execute(_SAVE_CHECK, key | {"level": level, "match": match, "sha": sha})
    return alert_row(await _read(session, key))


async def clip_source(session: AsyncSession, site_id: str, review_id: str) -> FrigateSite | None:
    """The Frigate holding this alert's clip. None if VigiSift has no such alert."""
    row = (
        await session.execute(
            text(
                "SELECT s.id, s.api_url FROM camera_alerts a JOIN sites s ON s.id = a.site_id"
                " WHERE a.site_id = :site AND a.review_id = :review"
            ),
            {"site": site_id, "review": review_id},
        )
    ).first()
    return FrigateSite(row.id, row.api_url) if row else None


async def list_sites(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(text("SELECT id, name, ui_url FROM sites ORDER BY name"))
    return [dict(row._mapping) for row in rows]


_UPSERT_SITE = text(
    "INSERT INTO sites (id, name, ui_url, api_url) VALUES (:id, :name, :ui, :api)"
    " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name,"
    " ui_url = EXCLUDED.ui_url, api_url = EXCLUDED.api_url"
)


async def upsert_site(
    db: AsyncSession | AsyncConnection, site_id: str, name: str, ui_url: str, api_url: str
) -> None:
    """Register a site or update its name and addresses (the reconcile mark is kept)."""
    await db.execute(_UPSERT_SITE, {"id": site_id, "name": name, "ui": ui_url, "api": api_url})


# Unreviewed alerts older than `cutoff`, and every alert older than `reviewed_cutoff`. Verdicts go
# with their alert (ON DELETE CASCADE): the longer cutoff keeps them as eval labels for a while.
_DELETE_EXPIRED = text(
    "DELETE FROM camera_alerts WHERE (site_id, review_id) IN ("
    " SELECT a.site_id, a.review_id FROM camera_alerts a"
    " WHERE a.started_at < :reviewed_cutoff OR (a.started_at < :cutoff AND NOT EXISTS ("
    "  SELECT 1 FROM alert_verdicts v WHERE v.site_id = a.site_id AND v.review_id = a.review_id))"
    " LIMIT :limit)"
)


async def delete_expired(
    session: AsyncSession, *, cutoff: datetime, reviewed_cutoff: datetime, limit: int
) -> int:
    """Delete up to `limit` expired alerts. Returns how many went."""
    result = await session.execute(
        _DELETE_EXPIRED, {"cutoff": cutoff, "reviewed_cutoff": reviewed_cutoff, "limit": limit}
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
