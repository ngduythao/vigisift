"""Re-check alerts from text, against the camera's rules.

Frigate's VLM describes scenes fairly well but scores very cautiously (see docs/03-eval.md).
VigiSift holds the rules, so it asks one easier question: "which indicator in the
rules does this description match?". The effective level is the higher of the two scores.
"""

import asyncio
import logging

from vigisift.domain.rules import (
    check_prompt,
    from_dict,
    indicator_choices,
    match_level,
    review_report,
)
from vigisift.features.alerts import repository as alerts
from vigisift.features.alerts.handlers import alert_event
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
        async with session() as db:
            row = await alerts.next_to_check(db)
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
            alert = await alerts.save_check(
                db,
                row.site_id,
                row.review_id,
                level=match.level,
                match=match.indicator,
                sha=row.sha,
            )
        await events.publish(alert_event(alert))
        checked += 1
    return checked


async def check_forever(
    session: SessionScope, picker: IndicatorPicker, events: EventBus, interval_sec: float
) -> None:
    while True:
        try:
            # A full pass means more are waiting: continue; rest only when the queue is empty.
            if await check_pending(session, picker, events) == BATCH:
                continue
        except Exception:
            log.exception("rule re-check loop failed")
        await asyncio.sleep(interval_sec)
