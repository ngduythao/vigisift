"""Build review payloads like Frigate 0.18 (`frigate/review/maintainer.py`,
`frigate/comms/dispatcher.py`): each message has `type`, `before`, `after`."""

import json
import time
from typing import Any

# An hour ago: recent enough for /alerts's default window, old enough to be
# outside a window of a few minutes.
START = float(int(time.time()) - 3600)

SUMMARY: dict[str, Any] = {
    "observations": [
        "A person walks into the aisle between two shelves.",
        "The person picks up a small box from the shelf and looks at it closely.",
        "The person puts the box in a jacket pocket and walks out.",
    ],
    "scene": (
        "A person walks into the aisle between two shelves, stops at the second shelf and picks "
        "up a small box. After looking around, the person puts the box in a jacket pocket rather "
        "than the basket, then walks toward the exit without passing the checkout."
    ),
    "title": "Person putting goods in a pocket",
    "shortSummary": "A person puts a small box in a jacket pocket and walks to the exit.",
    "confidence": 0.8,
    "potential_threat_level": 1,
    "other_concerns": None,
    "time": None,
}


def segment(
    review_id: str = "1761800000.0-abc123",
    *,
    camera: str = "shop_aisle",
    severity: str = "detection",
    start: float = START,
    end: float | None = None,
    objects: tuple[str, ...] = ("person",),
    zones: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "detections": ["1761800000.1-det001"],
        "objects": list(objects),
        "sub_labels": [],
        "zones": list(zones),
        "audio": [],
    }
    if metadata is not None:
        data["metadata"] = metadata
    return {
        "id": review_id,
        "camera": camera,
        "start_time": start,
        "end_time": end,
        "severity": severity,
        "thumb_path": f"/media/frigate/clips/review/thumb-{camera}-{review_id}.webp",
        "data": data,
    }


def message(kind: str, after: dict[str, Any]) -> str:
    return json.dumps({"type": kind, "before": after, "after": after})


def lifecycle(review_id: str) -> dict[str, str]:
    """Four reports from Frigate about one review, in the order Frigate sends them.

    The genai message carries times widened by 5 seconds at each end, as Frigate does
    in `image_source: recordings` mode.
    """
    return {
        "new": message("new", segment(review_id)),
        "update": message("update", segment(review_id, severity="alert", zones=("aisle",))),
        "end": message(
            "end", segment(review_id, severity="alert", zones=("aisle",), end=START + 12)
        ),
        "genai": message(
            "genai",
            segment(
                review_id,
                severity="alert",
                zones=("aisle",),
                start=START - 5,
                end=START + 17,
                metadata=SUMMARY,
            ),
        ),
    }
