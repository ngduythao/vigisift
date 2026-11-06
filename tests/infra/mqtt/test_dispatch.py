import asyncio
import logging

import pytest

from vigisift.infra.mqtt.client import Subscription, dispatch


async def test_a_failing_handler_is_logged_not_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    async def broken(_topic: str, _payload: str) -> None:
        raise RuntimeError("db down")

    subscription = Subscription("frigate/+/reviews", broken, None, asyncio.get_running_loop())

    with caplog.at_level(logging.ERROR):
        dispatch([subscription], "frigate/demo/reviews", "{}")
        await asyncio.sleep(0.1)

    assert "frigate/demo/reviews" in caplog.text
    assert "db down" in caplog.text
