"""Contract topic MQTT.

- Devices and server: `device/<id>/<kind>`.
- Each site's Frigate: `frigate/<site_id>/...`, set by `mqtt.topic_prefix`
  in that site's Frigate config. The ACL lets a site's user write only under
  its own prefix, so the site in a topic is the real site.
"""

from vigisift.domain.alert import SITE_ID

TELEMETRY = "device/+/telemetry"
STATUS = "device/+/status"
ACK = "device/+/ack"

FRIGATE_PREFIX = "frigate"
FRIGATE_REVIEWS = f"{FRIGATE_PREFIX}/+/reviews"


class InvalidTopicError(Exception):
    pass


def device_id(topic: str, kind: str) -> str:
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != "device" or parts[2] != kind or not parts[1]:
        raise InvalidTopicError(f"topic is not device/<id>/{kind}: {topic}")
    return parts[1]


def command_topic(device_id: str) -> str:
    return f"device/{device_id}/command"


def frigate_topic_prefix(site_id: str) -> str:
    """The `mqtt.topic_prefix` value for a site's Frigate."""
    return f"{FRIGATE_PREFIX}/{site_id}"


def frigate_site(topic: str, kind: str) -> str:
    parts = topic.split("/")
    if (
        len(parts) != 3
        or parts[0] != FRIGATE_PREFIX
        or parts[2] != kind
        or not SITE_ID.fullmatch(parts[1])
    ):
        raise InvalidTopicError(f"topic is not {FRIGATE_PREFIX}/<site>/{kind}: {topic}")
    return parts[1]
