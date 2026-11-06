"""Each camera's "suspicious" rules, written in words, then built into a VLM prompt.

Rules are data, not code: adding a site (a bakery warehouse, a watch store...)
only needs new rules, with nothing to change or redeploy.
"""

import re
from dataclasses import dataclass
from typing import Any

MAX_ITEMS = 12
MAX_ITEM_LENGTH = 200
MAX_CONCERNS = 10

# Always last in the prompt, and admins can't edit it: score only visible actions,
# and never use appearance as a signal. This is a rule of the system, not of
# any one site.
GUIDANCE = (
    "### Assessment Guidance\n"
    "Judge only actions that are visible in the frames. Never infer intent from appearance,"
    " clothing, age, gender, or ethnicity. Apply the indicators literally: if any frame shows"
    " an action listed under Level 1 or Level 2, assign at least that level, even when the"
    " rest of the sequence looks ordinary and the person stays calm. Assign Level 0 only when"
    " none of the listed indicators is visible. If something important cannot be seen, say"
    " so in the scene."
)


class InvalidRulesError(ValueError):
    pass


@dataclass(frozen=True)
class CameraRules:
    scene: str
    normal: tuple[str, ...]
    suspicious: tuple[str, ...]
    critical: tuple[str, ...]
    concerns: tuple[str, ...] = ()


def from_dict(raw: dict[str, Any]) -> CameraRules:
    """Rules as stored in the DB (`asdict`) or as in the eval set's JSON file."""
    return CameraRules(
        scene=raw["scene"],
        normal=tuple(raw["normal"]),
        suspicious=tuple(raw["suspicious"]),
        critical=tuple(raw.get("critical", ())),
        concerns=tuple(raw.get("concerns", ())),
    )


def _clean(item: str, field: str) -> str:
    # Each item is ONE bullet line: drop line breaks, `#` and leading markdown
    # characters, so an item can't open a new section in the prompt.
    value = re.sub(r"\s+", " ", item.replace("#", " ")).strip().lstrip("-*> ").strip()
    if not 3 <= len(value) <= MAX_ITEM_LENGTH:
        raise InvalidRulesError(f"{field}: each item must be 3 to {MAX_ITEM_LENGTH} characters")
    return value


def _items(values: tuple[str, ...], field: str, minimum: int, maximum: int) -> tuple[str, ...]:
    cleaned = tuple(_clean(value, field) for value in values)
    if not minimum <= len(cleaned) <= maximum:
        raise InvalidRulesError(f"{field}: needs {minimum} to {maximum} items")
    return cleaned


def validated(rules: CameraRules) -> CameraRules:
    return CameraRules(
        scene=_clean(rules.scene, "scene"),
        normal=_items(rules.normal, "normal", 1, MAX_ITEMS),
        suspicious=_items(rules.suspicious, "suspicious", 1, MAX_ITEMS),
        critical=_items(rules.critical, "critical", 0, MAX_ITEMS),
        concerns=_items(rules.concerns, "concerns", 0, MAX_CONCERNS),
    )


def activity_context(rules: CameraRules) -> str:
    """A prompt in the Level 0/1/2 frame Frigate uses for `activity_context_prompt`."""
    sections = [
        f"This camera watches {rules.scene.rstrip('.')}.",
        "### Normal Activity Indicators (Level 0)\n" + "\n".join(f"- {x}" for x in rules.normal),
        "### Suspicious Activity Indicators (Level 1)\n"
        + "\n".join(f"- {x}" for x in rules.suspicious),
    ]
    if rules.critical:
        sections.append(
            "### Critical Threat Indicators (Level 2)\n"
            + "\n".join(f"- {x}" for x in rules.critical)
        )
    sections.append(GUIDANCE)
    return "\n\n".join(sections)


# Re-check from text. Measured on the eval set: the vision model describes "putting
# goods in a bag" correctly but almost always scores level 0 when it must look and
# score at once, and prompt changes don't help. Asking separately "which indicator
# in the rules does this description match" works well on a small model in ~1 second.
# The model only PICKS an indicator; code derives the level from where it sits.
NO_MATCH = "none"


@dataclass(frozen=True)
class RuleMatch:
    level: int
    # The matched indicator, verbatim from the rules; None when nothing matched.
    indicator: str | None


def indicator_choices(rules: CameraRules) -> list[str]:
    """Valid choices, highest level first. A duplicate across levels stays at the higher one."""
    choices = list(dict.fromkeys((*rules.critical, *rules.suspicious, *rules.normal)))
    return [*choices, NO_MATCH]


def match_level(rules: CameraRules, choice: str) -> RuleMatch:
    if choice in rules.critical:
        return RuleMatch(2, choice)
    if choice in rules.suspicious:
        return RuleMatch(1, choice)
    if choice in rules.normal:
        return RuleMatch(0, choice)
    return RuleMatch(0, None)


def review_report(
    title: str | None, summary: str | None, scene: str | None, observations: tuple[str, ...]
) -> str:
    """The VLM's description of a review, gathered as text to compare with the rules."""
    lines = [f"Title: {title}" if title else "", f"Summary: {summary}" if summary else ""]
    if scene:
        lines.append(f"Scene: {scene}")
    lines += [f"- {item}" for item in observations]
    return "\n".join(line for line in lines if line)


def check_prompt(rules: CameraRules, report: str) -> str:
    return (
        "You check a security camera report against the rules for that camera.\n\n"
        f"## Rules\n{activity_context(rules)}\n\n"
        f"## Report\n{report}\n\n"
        "## Task\n"
        "Choose the one indicator from the rules that the report says happened, checking"
        " Level 2 first, then Level 1, then Level 0. A report that describes the action in"
        ' other words still matches. Choose "none" if the report does not describe any'
        " listed action. Judge only what the report says; do not guess what it leaves out."
    )


# Starting points for common site types. Admins adjust them for each camera.
PRESETS: dict[str, CameraRules] = {
    "retail_store": CameraRules(
        scene="an aisle inside a retail store during opening hours",
        normal=(
            "Customers walking, browsing, or picking up items to look at them, then putting"
            " them back or into a basket or cart",
            "Staff restocking shelves, moving stock, or tidying displays",
            "People shopping together and talking",
        ),
        suspicious=(
            "Putting merchandise into their own pocket, bag, or clothing instead of a basket or"
            " cart, even calmly",
            "Removing packaging or security tags from merchandise",
            "Repeatedly looking around for staff or cameras while handling merchandise",
            "Staying in one spot handling merchandise for a long time without a clear purpose",
        ),
        critical=(
            "Weapons visible",
            "Physical aggression or violence",
            "Grabbing large amounts of merchandise and running",
        ),
    ),
    "warehouse": CameraRules(
        scene="the stock room of a bakery warehouse, used only by staff",
        normal=(
            "Staff carrying, stacking, counting, or loading boxes of stock",
            "Staff checking paperwork or a tablet",
            "Delivery workers bringing stock in through the loading door",
        ),
        suspicious=(
            "Eating or drinking products taken from the stock shelves",
            "Putting stock or cash into a personal bag, pocket, or jacket",
            "Opening the cash box or safe when no second person is present",
            "Anyone in the stock room outside working hours",
        ),
        critical=(
            "Forcing a door, lock, cash box, or safe",
            "Physical aggression or violence",
        ),
        concerns=("cash box left open",),
    ),
    "jewelry_store": CameraRules(
        scene="the display counters of a watch and jewelry store",
        normal=(
            "Customers looking at items while a staff member stands across the counter",
            "Staff taking items out of a case for a customer and putting them back",
            "Staff cleaning or rearranging display cases",
        ),
        suspicious=(
            "A customer holding an item while no staff member is at the counter",
            "An item moving from the counter into a pocket, sleeve, or bag",
            "A display case left open with no staff member nearby",
            "Two people working together so one distracts the staff while the other handles items",
        ),
        critical=(
            "Breaking or forcing a display case",
            "Weapons visible",
            "Grabbing items and running",
        ),
    ),
}
