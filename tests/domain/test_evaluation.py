import pytest

from vigisift.domain.evaluation import Clip, Observed, Outcome, assign, score, spills

CLIPS = [
    Clip("theft-1", True, offset_sec=30, duration_sec=20),
    Clip("normal-1", False, offset_sec=100, duration_sec=20),
    Clip("theft-2", True, offset_sec=170, duration_sec=20),
    Clip("normal-2", False, offset_sec=240, duration_sec=20),
]


def test_alerts_are_assigned_to_the_clip_playing_when_they_started() -> None:
    observed = [
        Observed("a", 33, 1),
        Observed("b", 105, 0),
        Observed("c", 172, None),
        Observed("stray", 400, 2),  # after the video: belongs to no clip
    ]

    results = {r.clip.name: r for r in assign(CLIPS, observed, tolerance_sec=5)}

    assert results["theft-1"].review_ids == ("a",)
    assert results["normal-1"].outcome is Outcome.SUMMARISED
    assert results["theft-2"].outcome is Outcome.NO_SUMMARY
    assert results["normal-2"].outcome is Outcome.NO_ALERT


def test_the_tolerance_covers_detection_lag_after_a_clip_ends() -> None:
    [first, *_] = assign(CLIPS, [Observed("late", 53, 1)], tolerance_sec=5)

    assert first.review_ids == ("late",)


def test_a_clip_is_flagged_by_its_most_suspicious_alert() -> None:
    observed = [Observed("a", 31, 0), Observed("b", 40, 2)]

    [first, *_] = assign(CLIPS, observed, tolerance_sec=5)

    assert first.max_threat == 2 and first.flagged


def test_the_report_counts_both_kinds_of_mistake_separately() -> None:
    observed = [
        Observed("tp", 31, 1, summary_delay_sec=50),
        Observed("fp", 101, 1, summary_delay_sec=70),
        Observed("fn", 171, 0, summary_delay_sec=60),
    ]
    report = score(assign(CLIPS, observed, tolerance_sec=5), observed)

    assert (report.true_positives, report.false_positives) == (1, 1)
    assert (report.false_negatives, report.true_negatives) == (1, 1)
    assert report.precision == 0.5
    assert report.recall == 0.5
    assert report.specificity == 0.5
    # 1 false alarm in 40 seconds of normal video.
    assert report.false_alarms_per_hour == pytest.approx(90)
    assert report.median_summary_delay_sec == 60
    assert report.outcomes == {"summarised": 3, "no_alert": 1}


def test_no_alert_and_no_summary_both_count_as_the_ai_staying_quiet() -> None:
    report = score(assign(CLIPS, [Observed("x", 171, None)], tolerance_sec=5), [])

    assert report.false_negatives == 2, "the operator sees nothing, whatever the reason"
    assert report.precision is None


def test_an_alert_still_open_when_the_next_clip_starts_is_reported() -> None:
    observed = [
        Observed("ok", 31, 1, end_sec=80),
        Observed("merged", 105, 0, end_sec=175),  # spills into theft-2 (starts at 170)
        # Starts slightly before its clip: still theft-2's, not a spill.
        Observed("early", 167, 1, end_sec=200),
        Observed("open", 241, None),  # not ended: unknown, not reported
    ]
    results = assign(CLIPS, observed, tolerance_sec=5)

    assert spills(results, observed) == ["merged"]
