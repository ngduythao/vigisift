import logging

from vigisift.features import topics
from vigisift.features.alerts import repository
from vigisift.features.alerts.payload import InvalidReviewError, parse_review_message
from vigisift.features.alerts.service import alert_writer
from vigisift.runtime import Runtime

log = logging.getLogger(__name__)


async def on_review(runtime: Runtime, topic: str, raw: str) -> None:
    try:
        site_id = topics.frigate_site(topic, "reviews")
        update = parse_review_message(site_id, raw)
    except (topics.InvalidTopicError, InvalidReviewError) as error:
        log.warning("dropped review on %s: %s", topic, error)
        return

    try:
        async with alert_writer(runtime.session, runtime.events) as writer:
            await writer.record(update)
    except repository.UnknownSiteError:
        log.warning("dropped review %s: site %s is not registered", update.review_id, site_id)
