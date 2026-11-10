"""Re-check alerts from text, against the camera's rules.

Frigate's VLM describes scenes fairly well but scores very cautiously (see docs/03-eval.md).
VigiSift holds the rules, so it asks one easier question: "which indicator in the
rules does this description match?". The effective level is the higher of the two scores.
"""

import logging

from vigisift.domain.rules import (
    check_prompt,
    from_dict,
    indicator_choices,
    match_level,
    review_report,
)
from vigisift.features.alerts import repository as alerts
from vigisift.features.alerts.service import alert_writer
from vigisift.ports.cache import EventBus
from vigisift.ports.llm import ChatUnavailableError
from vigisift.ports.rule_check import IndicatorPicker
from vigisift.runtime import SessionScope

log = logging.getLogger(__name__)

BATCH = 20


async def check_pending(
    session: SessionScope, picker: IndicatorPicker, events: EventBus, *, limit: int = BATCH
) -> int:
    """Re-check up to `limit` waiting alerts. Returns the number checked.

    If the model can't answer, stop this pass and leave the alert: the next pass
    checks it again, without recording a fake "no match".
    """
    checked = 0
    while checked < limit:
        async with alert_writer(session, events) as writer:
            row = await alerts.next_to_check(writer.db)
            if row is None:
                break
            rules = from_dict(row.rules)
            report = review_report(row.title, row.summary, row.scene, tuple(row.observations or ()))
            try:
                choice = await picker.pick(check_prompt(rules, report), indicator_choices(rules))
            except ChatUnavailableError as error:
                log.warning("can't re-check %s/%s yet: %s", row.site_id, row.review_id, error)
                break
            match = match_level(rules, choice)
            await writer.save_check(
                row.site_id,
                row.review_id,
                level=match.level,
                match=match.indicator,
                sha=row.sha,
            )
        checked += 1
    return checked
