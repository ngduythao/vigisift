from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from vigisift.dependencies import RuntimeDep, SessionDep
from vigisift.domain.rules import PRESETS, CameraRules, InvalidRulesError, validated
from vigisift.features.auth.guard import AdminOperator
from vigisift.features.rules import repository
from vigisift.features.rules.apply import push_pending
from vigisift.ports.frigate import FrigateSite

router = APIRouter(tags=["rules"])

Camera = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class RulesBody(BaseModel):
    scene: str = Field(max_length=300)
    normal: list[str] = Field(max_length=20)
    suspicious: list[str] = Field(max_length=20)
    critical: list[str] = Field(default_factory=list, max_length=20)
    concerns: list[str] = Field(default_factory=list, max_length=20)

    def to_domain(self) -> CameraRules:
        return CameraRules(
            scene=self.scene,
            normal=tuple(self.normal),
            suspicious=tuple(self.suspicious),
            critical=tuple(self.critical),
            concerns=tuple(self.concerns),
        )


class RulesView(BaseModel):
    site_id: str
    camera: str
    rules: RulesBody
    prompt: str
    applied: bool
    applied_at: datetime | None
    apply_error: str | None
    updated_at: datetime
    updated_by: str | None


def _view(row: dict[str, Any]) -> RulesView:
    return RulesView(
        site_id=row["site_id"],
        camera=row["camera"],
        rules=RulesBody(**row["rules"]),
        prompt=row["prompt"],
        applied=row["applied_sha"] == row["sha"],
        applied_at=row["applied_at"],
        apply_error=row["apply_error"],
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
    )


@router.get("/rule-presets")
async def presets() -> dict[str, RulesBody]:
    return {
        name: RulesBody(
            scene=preset.scene,
            normal=list(preset.normal),
            suspicious=list(preset.suspicious),
            critical=list(preset.critical),
            concerns=list(preset.concerns),
        )
        for name, preset in PRESETS.items()
    }


@router.get("/rules")
async def list_rules(session: SessionDep) -> list[RulesView]:
    return [_view(row) for row in await repository.list_all(session)]


@router.put("/sites/{site_id}/cameras/{camera}/rules")
async def put_rules(
    site_id: str,
    camera: Camera,
    body: RulesBody,
    admin: AdminOperator,
    runtime: RuntimeDep,
    session: SessionDep,
) -> RulesView:
    """Save the rules and push them to the site's Frigate right away. Admin only.

    Save first, push after: with Frigate off the rules are still kept and reconcile
    pushes them later; the push result is in `applied` and `apply_error`.
    """
    try:
        rules = validated(body.to_domain())
    except InvalidRulesError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
    site = (
        await session.execute(text("SELECT id, api_url FROM sites WHERE id = :id"), {"id": site_id})
    ).one_or_none()
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="site_not_found")
    await repository.save(session, site_id, camera, rules, admin.id)
    await session.commit()
    await push_pending(runtime.session, runtime.frigate, FrigateSite(site.id, site.api_url), camera)
    row = await repository.get(session, site_id, camera)
    assert row is not None
    return _view(row)
