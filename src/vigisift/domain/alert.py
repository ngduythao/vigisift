import re
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum

# A site id goes straight into MQTT topics, Mosquitto users and URLs, so only
# lowercase letters, digits and hyphens. Same rule as the CHECK in schema.sql.
SITE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class Severity(StrEnum):
    """Frigate splits reviews into alert (important objects) and detection."""

    ALERT = "alert"
    DETECTION = "detection"


class ThreatLevel(IntEnum):
    """The suspicion level Frigate's VLM assigned (`potential_threat_level`)."""

    NORMAL = 0
    SUSPICIOUS = 1
    CRITICAL = 2


@dataclass(frozen=True)
class AlertSummary:
    """The VLM summary of a review. It arrives last, and may never arrive."""

    title: str
    summary: str
    scene: str
    confidence: float
    threat_level: ThreatLevel
    other_concerns: tuple[str, ...]
    # Each thing the VLM saw, in time order; VigiSift compares these with the rules.
    observations: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlertUpdate:
    """One validated report from Frigate about a review.

    Each report carries only part of the truth (review still open, just closed, or
    just summarised), so only merging them produces the complete alert.
    """

    site_id: str
    review_id: str
    camera: str
    severity: Severity
    objects: tuple[str, ...]
    zones: tuple[str, ...]
    started_at: datetime
    ended_at: datetime | None
    # Times taken from the summary message: Frigate widened them, so they're approximate.
    times_approximate: bool
    summary: AlertSummary | None


def review_link(ui_url: str, review_id: str) -> str:
    """Link to this review in Frigate's UI, the same link its notifications use."""
    return f"{ui_url.rstrip('/')}/review?id={review_id}"
