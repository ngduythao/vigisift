"""An alert as the API shows it: one row of the `alert_list` view, plus its Frigate link."""

from datetime import datetime

from pydantic import BaseModel

from vigisift.domain.alert import Severity
from vigisift.domain.operator import Verdict


class AlertView(BaseModel):
    site_id: str
    review_id: str
    camera: str
    severity: Severity
    objects: list[str]
    zones: list[str]
    started_at: datetime
    ended_at: datetime | None
    times_approximate: bool
    # The level Frigate gave (VLM looking at images), the level VigiSift re-checked by the
    # camera's rules, and the effective level (higher of the two) the queue uses.
    threat_level: int | None
    rule_level: int | None = None
    rule_match: str | None = None
    level: int | None = None
    title: str | None
    summary: str | None
    scene: str | None
    confidence: float | None
    other_concerns: list[str] | None
    observations: list[str] | None = None
    link: str
    # When VigiSift first saw the alert, the latest merge (reconcile also merges, so
    # NOT when the summary arrived), and when the summary first arrived.
    first_seen_at: datetime | None = None
    updated_at: datetime | None = None
    summarised_at: datetime | None = None
    # The operator's latest verdict, if any.
    verdict: Verdict | None = None
    verdict_by: str | None = None
    verdict_at: datetime | None = None
