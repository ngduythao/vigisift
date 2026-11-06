import pytest

from vigisift.features.topics import InvalidTopicError, device_id


def test_a_malformed_topic_is_refused() -> None:
    with pytest.raises(InvalidTopicError):
        device_id("device/lamp-1", "telemetry")
