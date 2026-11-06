"""Score the whole system on labelled clips: video → Frigate → VLM → VigiSift.

The clips are joined into one long video with quiet gaps, then played through a
fake camera. Each alert belongs to the clip playing when it started. A clip counts
as "AI flagged suspicious" if at least one of its alerts scored level ≥ 1.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from statistics import median


class Outcome(StrEnum):
    # Frigate raised no alert while the clip played (saw no person).
    NO_ALERT = "no_alert"
    # Alerts exist but none has a summary (VLM error or timeout).
    NO_SUMMARY = "no_summary"
    SUMMARISED = "summarised"


@dataclass(frozen=True)
class Clip:
    name: str
    suspicious: bool
    # Position in the joined video, in seconds from its start.
    offset_sec: float
    duration_sec: float


@dataclass(frozen=True)
class Observed:
    """An alert VigiSift received, with times measured from the start of the joined video."""

    review_id: str
    start_sec: float
    threat_level: int | None
    # From the review ending to the summary arriving; None if unknown.
    summary_delay_sec: float | None = None
    end_sec: float | None = None


@dataclass(frozen=True)
class ClipResult:
    clip: Clip
    outcome: Outcome
    max_threat: int | None
    review_ids: tuple[str, ...]

    @property
    def flagged(self) -> bool:
        return self.max_threat is not None and self.max_threat >= 1


def assign(clips: list[Clip], observed: list[Observed], tolerance_sec: float) -> list[ClipResult]:
    """Assign each alert to the clip playing when it started (with `tolerance_sec`
    at both ends for detection delay). Alerts belonging to no clip are ignored."""
    by_clip: dict[str, list[Observed]] = {clip.name: [] for clip in clips}
    for alert in observed:
        for clip in clips:
            start = clip.offset_sec - tolerance_sec
            end = clip.offset_sec + clip.duration_sec + tolerance_sec
            if start <= alert.start_sec <= end:
                by_clip[clip.name].append(alert)
                break
    results = []
    for clip in clips:
        alerts = by_clip[clip.name]
        levels = [a.threat_level for a in alerts if a.threat_level is not None]
        if not alerts:
            outcome = Outcome.NO_ALERT
        elif not levels:
            outcome = Outcome.NO_SUMMARY
        else:
            outcome = Outcome.SUMMARISED
        results.append(
            ClipResult(
                clip,
                outcome,
                max(levels) if levels else None,
                tuple(a.review_id for a in alerts),
            )
        )
    return results


def spills(results: list[ClipResult], observed: list[Observed]) -> list[str]:
    """Alerts still open when the clip AFTER theirs has started: two clips can share
    one review, and assigning by start time would score both wrong. The gap between
    clips must be long enough for this list to be empty."""
    ends = {o.review_id: o.end_sec for o in observed}
    ordered = sorted(results, key=lambda r: r.clip.offset_sec)
    found = []
    for result, following in pairwise(ordered):
        for review_id in result.review_ids:
            end = ends.get(review_id)
            if end is not None and end > following.clip.offset_sec:
                found.append(review_id)
    return found


@dataclass
class Report:
    clips: int = 0
    suspicious_clips: int = 0
    normal_clips: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    true_negatives: int = 0
    normal_video_hours: float = 0.0
    outcomes: dict[str, int] = field(default_factory=dict)
    summary_delays_sec: list[float] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        flagged = self.true_positives + self.false_positives
        return self.true_positives / flagged if flagged else None

    @property
    def recall(self) -> float | None:
        return self.true_positives / self.suspicious_clips if self.suspicious_clips else None

    @property
    def specificity(self) -> float | None:
        return self.true_negatives / self.normal_clips if self.normal_clips else None

    @property
    def false_alarms_per_hour(self) -> float | None:
        """Normal clips flagged suspicious, per hour of normal video.

        Counts clips (each falsely flagged clip once), not alerts."""
        return self.false_positives / self.normal_video_hours if self.normal_video_hours else None

    @property
    def median_summary_delay_sec(self) -> float | None:
        return median(self.summary_delays_sec) if self.summary_delays_sec else None


def score(results: list[ClipResult], observed: list[Observed]) -> Report:
    """No alert and no summary both count as "AI didn't flag": to an operator they
    are the same. `outcomes` keeps them apart to show which layer failed."""
    report = Report(clips=len(results))
    assigned = {rid for result in results for rid in result.review_ids}
    for result in results:
        key = result.outcome.value
        report.outcomes[key] = report.outcomes.get(key, 0) + 1
        if result.clip.suspicious:
            report.suspicious_clips += 1
            if result.flagged:
                report.true_positives += 1
            else:
                report.false_negatives += 1
        else:
            report.normal_clips += 1
            report.normal_video_hours += result.clip.duration_sec / 3600
            if result.flagged:
                report.false_positives += 1
            else:
                report.true_negatives += 1
    report.summary_delays_sec = [
        o.summary_delay_sec
        for o in observed
        if o.review_id in assigned and o.summary_delay_sec is not None
    ]
    return report
