import hashlib
import json
import logging
from dataclasses import asdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.domain.rules import (
    CameraRules,
    InvalidRulesError,
    activity_context,
    from_dict,
    validated,
)

log = logging.getLogger(__name__)


def _sha(prompt: str, concerns: list[str]) -> str:
    return hashlib.sha256(json.dumps([prompt, concerns]).encode()).hexdigest()


async def save(
    session: AsyncSession, site_id: str, camera: str, rules: CameraRules, operator_id: int
) -> None:
    prompt = activity_context(rules)
    concerns = list(rules.concerns)
    await session.execute(
        text(
            "INSERT INTO camera_rules (site_id, camera, rules, prompt, concerns, sha, updated_by)"
            " VALUES (:site, :camera, CAST(:rules AS jsonb), :prompt, :concerns, :sha, :by)"
            " ON CONFLICT (site_id, camera) DO UPDATE SET rules = EXCLUDED.rules,"
            " prompt = EXCLUDED.prompt, concerns = EXCLUDED.concerns, sha = EXCLUDED.sha,"
            " apply_error = NULL, updated_by = EXCLUDED.updated_by, updated_at = now()"
        ),
        {
            "site": site_id,
            "camera": camera,
            "rules": json.dumps(asdict(rules), ensure_ascii=False),
            "prompt": prompt,
            "concerns": concerns,
            "sha": _sha(prompt, concerns),
            "by": operator_id,
        },
    )


async def rerender(session: AsyncSession, site_id: str) -> list[str]:
    """Rebuild prompts from stored rules, using the CURRENT code. Returns changed cameras.

    A prompt is the rules plus the system's fixed part (Level frame, GUIDANCE). When the
    fixed part changes the old rules must follow: changing the prompt and sha is enough for
    reconcile to push the new version to Frigate, as when an admin saves.
    """
    rows = await session.execute(
        text(
            "SELECT site_id, camera, rules, prompt, concerns FROM camera_rules"
            " WHERE site_id = :site FOR UPDATE"
        ),
        {"site": site_id},
    )
    changed = []
    for row in rows.all():
        try:
            rules = validated(from_dict(row.rules))
        except (InvalidRulesError, KeyError, TypeError) as error:
            log.warning("rules %s/%s can't be rebuilt: %s", row.site_id, row.camera, error)
            continue
        prompt = activity_context(rules)
        if prompt == row.prompt:
            continue
        await session.execute(
            text(
                "UPDATE camera_rules SET prompt = :prompt, sha = :sha, apply_error = NULL"
                " WHERE site_id = :site AND camera = :camera"
            ),
            {
                "prompt": prompt,
                "sha": _sha(prompt, list(row.concerns)),
                "site": row.site_id,
                "camera": row.camera,
            },
        )
        changed.append(f"{row.site_id}/{row.camera}")
    return changed


_COLUMNS = (
    "r.site_id, r.camera, r.rules, r.prompt, r.concerns, r.sha, r.applied_sha, r.applied_at,"
    " r.apply_error, r.updated_at, o.username AS updated_by"
)


async def get(session: AsyncSession, site_id: str, camera: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                f"SELECT {_COLUMNS} FROM camera_rules r"
                " LEFT JOIN operators o ON o.id = r.updated_by"
                " WHERE r.site_id = :site AND r.camera = :camera"
            ),
            {"site": site_id, "camera": camera},
        )
    ).one_or_none()
    return dict(row._mapping) if row is not None else None


async def list_all(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            f"SELECT {_COLUMNS} FROM camera_rules r LEFT JOIN operators o ON o.id = r.updated_by"
            " ORDER BY r.site_id, r.camera"
        )
    )
    return [dict(row._mapping) for row in rows]


# Waiting to push: Frigate hasn't received the latest version, and Frigate hasn't
# rejected this version (pushing a rejected one is useless; wait for an admin edit).
_PENDING = "applied_sha IS DISTINCT FROM sha AND apply_error IS NULL"


async def pending_cameras(session: AsyncSession, site_id: str) -> list[str]:
    rows = await session.execute(
        text(
            f"SELECT camera FROM camera_rules WHERE site_id = :site AND {_PENDING} ORDER BY camera"
        ),
        {"site": site_id},
    )
    return [row.camera for row in rows]


async def lock_pending(session: AsyncSession, site_id: str, camera: str) -> dict[str, Any] | None:
    """Lock a camera's rule row until the transaction ends, if it's still waiting to push.

    Two simultaneous pushers (the save route and the reconcile loop, maybe on two
    instances) must queue here; the later one reads again and sees the latest version,
    so it never pushes an old version over Frigate.
    """
    row = (
        await session.execute(
            text(
                "SELECT camera, prompt, concerns, sha FROM camera_rules"
                f" WHERE site_id = :site AND camera = :camera AND {_PENDING} FOR UPDATE"
            ),
            {"site": site_id, "camera": camera},
        )
    ).one_or_none()
    return dict(row._mapping) if row is not None else None


async def forget_applied(session: AsyncSession, site_id: str, camera: str, sha: str) -> None:
    """Frigate is using something other than what was pushed: mark as unapplied to push again.

    Only if the applied version is still the one just compared: a newer admin save is left alone.
    """
    await session.execute(
        text(
            "UPDATE camera_rules SET applied_sha = NULL"
            " WHERE site_id = :site AND camera = :camera AND applied_sha = :sha"
        ),
        {"site": site_id, "camera": camera, "sha": sha},
    )


async def applied(session: AsyncSession, site_id: str) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT camera, prompt, concerns, sha FROM camera_rules"
            " WHERE site_id = :site AND applied_sha = sha"
        ),
        {"site": site_id},
    )
    return [dict(row._mapping) for row in rows]


async def mark(
    session: AsyncSession, site_id: str, camera: str, sha: str, error: str | None
) -> None:
    """Record a push result. Marks applied only if the rules weren't edited during the push."""
    if error is None:
        await session.execute(
            text(
                "UPDATE camera_rules SET applied_sha = :sha, applied_at = now(), apply_error = NULL"
                " WHERE site_id = :site AND camera = :camera AND sha = :sha"
            ),
            {"site": site_id, "camera": camera, "sha": sha},
        )
    else:
        await session.execute(
            text(
                "UPDATE camera_rules SET apply_error = :error"
                " WHERE site_id = :site AND camera = :camera AND sha = :sha"
            ),
            {"site": site_id, "camera": camera, "sha": sha, "error": error[:500]},
        )
