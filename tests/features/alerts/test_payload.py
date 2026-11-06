import json
from datetime import UTC, datetime

import pytest

from tests.features.alerts.frigate import START, SUMMARY, lifecycle, message, segment
from vigisift.domain.alert import Severity, ThreatLevel
from vigisift.features import topics
from vigisift.features.alerts.payload import (
    InvalidReviewError,
    parse_review_message,
    parse_review_segment,
)


def test_a_new_review_has_exact_times_and_no_summary() -> None:
    update = parse_review_message("demo", lifecycle("r-1")["new"])

    assert update.site_id == "demo"
    assert update.severity is Severity.DETECTION
    assert update.objects == ("person",)
    assert update.started_at == datetime.fromtimestamp(START, UTC)
    assert update.ended_at is None
    assert update.times_approximate is False
    assert update.summary is None


def test_a_genai_message_carries_the_summary_but_only_approximate_times() -> None:
    update = parse_review_message("demo", lifecycle("r-1")["genai"])

    assert update.summary is not None
    assert update.summary.threat_level is ThreatLevel.SUSPICIOUS
    assert update.summary.summary == SUMMARY["shortSummary"]
    assert update.times_approximate is True, "Frigate widened the times before sending the summary"


def test_unknown_fields_are_ignored() -> None:
    after = segment() | {"has_been_reviewed": False, "something_new": [1, 2]}

    update = parse_review_message("demo", message("new", after))

    assert update.review_id == after["id"]


def test_an_api_row_parses_like_the_mqtt_after_block() -> None:
    update = parse_review_segment("demo", segment(metadata=SUMMARY))

    assert update.summary is not None and update.times_approximate is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("severity", "panic"),
        ("camera", "shop/aisle"),
        ("id", "../../etc"),
        ("start_time", -1),
        ("end_time", "later"),
    ],
)
def test_a_malformed_segment_is_refused(field: str, value: object) -> None:
    after = segment() | {field: value}

    with pytest.raises(InvalidReviewError, match=field):
        parse_review_message("demo", message("new", after))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("potential_threat_level", 3),
        ("confidence", 1.5),
        ("title", ""),
        ("scene", "x" * 5000),
    ],
)
def test_a_summary_outside_its_bounds_is_refused(field: str, value: object) -> None:
    after = segment(metadata=SUMMARY | {field: value})

    with pytest.raises(InvalidReviewError):
        parse_review_message("demo", message("genai", after))


def test_an_end_before_the_start_is_refused() -> None:
    with pytest.raises(InvalidReviewError, match="end_time"):
        parse_review_message("demo", message("end", segment(end=START - 1)))


@pytest.mark.parametrize("raw", ["not json", json.dumps({"type": "new"}), json.dumps([])])
def test_garbage_is_refused_not_crashed_on(raw: str) -> None:
    with pytest.raises(InvalidReviewError):
        parse_review_message("demo", raw)


def test_an_unknown_message_type_is_refused() -> None:
    with pytest.raises(InvalidReviewError, match="type"):
        parse_review_message("demo", message("deleted", segment()))


def test_the_site_comes_from_the_topic() -> None:
    assert topics.frigate_site("frigate/kho-banh/reviews", "reviews") == "kho-banh"


@pytest.mark.parametrize(
    "topic",
    ["frigate/reviews", "frigate/Demo/reviews", "frigate/a/b/reviews", "frigate/demo/events"],
)
def test_a_topic_without_a_valid_site_is_refused(topic: str) -> None:
    with pytest.raises(topics.InvalidTopicError):
        topics.frigate_site(topic, "reviews")
