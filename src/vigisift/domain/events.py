from enum import StrEnum


class EventType(StrEnum):
    """The `type` of each event sent to the console over `/ws/events`.

    The page's event handler (`web/index.html`, `connectEvents`) switches on these same
    strings; tests/test_events.py checks the two lists match.
    """

    CAMERA_ALERT = "camera_alert"
    ALERT_VERDICT = "alert_verdict"
    PRESENCE = "presence"
    COMMAND = "command"
    TELEMETRY = "telemetry"
