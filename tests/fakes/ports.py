"""A fake for each port: runs in memory, recording what it received for tests to check."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from vigisift.infra.mqtt.client import Subscription, dispatch
from vigisift.ports.frigate import (
    FrigateClipMissingError,
    FrigateRejectedError,
    FrigateSite,
    FrigateUnavailableError,
)
from vigisift.ports.llm import ChatUnavailableError
from vigisift.ports.messaging import Handler


class FakeMqttClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.options: list[tuple[int, bool]] = []
        self.subscriptions: list[Subscription] = []

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        self.published.append((topic, payload))
        self.options.append((qos, retain))

    def subscribe(self, topic: str, handler: Handler, shared_group: str | None = None) -> None:
        self.subscriptions.append(
            Subscription(topic, handler, shared_group, asyncio.get_running_loop())
        )

    def close(self) -> None:
        pass

    def deliver(self, topic: str, payload: str) -> None:
        """Simulate a device sending a message to the broker."""
        dispatch(self.subscriptions, topic, payload)


class FakePresence:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    async def touch(self, device_id: str) -> None:
        self.seen.add(device_id)

    async def forget(self, device_id: str) -> None:
        self.seen.discard(device_id)

    async def online(self) -> set[str]:
        return set(self.seen)


class FakeTokenBudget:
    def __init__(self, daily_limit: int = 1_000_000) -> None:
        self.limit = daily_limit
        self.spent = 0

    async def remaining(self) -> int:
        return max(self.limit - self.spent, 0)

    async def spend(self, tokens: int) -> None:
        self.spent += tokens


class FakeFrigateApi:
    """A fake Frigate: returns reviews by the filter rules and order of `GET /api/review`."""

    def __init__(self, reviews: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.reviews_by_site = reviews or {}
        self.failing: set[str] = set()
        self.calls: list[tuple[str, float, float, int]] = []
        self.cameras = {"shop_aisle"}
        self.rules: dict[tuple[str, str], tuple[str, list[str]]] = {}
        self.pushes: list[tuple[str, str]] = []
        # Reading config fails on its own, while reading reviews still works.
        self.config_failing: set[str] = set()
        # Simulates a slow Frigate so two pushes overlap.
        self.push_delay_sec = 0.0
        # Cameras whose pushes always fail in delivery (timeout, 5xx).
        self.unreachable_cameras: set[str] = set()
        # review id → the chunks of its video.
        self.clips: dict[str, list[bytes]] = {}

    async def reviews(
        self, site: FrigateSite, *, after: float, before: float, limit: int
    ) -> list[dict[str, Any]]:
        self.calls.append((site.id, after, before, limit))
        if site.id in self.failing:
            raise FrigateUnavailableError(f"{site.id}: fake Frigate is off")
        rows = [
            row
            for row in self.reviews_by_site.get(site.id, [])
            if row["start_time"] < before and (row["end_time"] is None or row["end_time"] > after)
        ]
        return sorted(rows, key=lambda row: row["start_time"], reverse=True)[:limit]

    async def set_review_rules(
        self, site: FrigateSite, camera: str, *, prompt: str, concerns: list[str]
    ) -> None:
        self.pushes.append((site.id, camera))
        await asyncio.sleep(self.push_delay_sec)
        if site.id in self.failing or camera in self.unreachable_cameras:
            raise FrigateUnavailableError(f"{site.id}: fake Frigate is off")
        if camera not in self.cameras:
            raise FrigateRejectedError(f"{site.id}: camera {camera} is not in the config")
        self.rules[(site.id, camera)] = (prompt, concerns)

    async def review_rules(self, site: FrigateSite) -> dict[str, tuple[str, list[str]]]:
        if site.id in self.failing | self.config_failing:
            raise FrigateUnavailableError(f"{site.id}: fake Frigate is off")
        return {camera: self.rules.get((site.id, camera), ("", [])) for camera in self.cameras}

    async def review_clip(self, site: FrigateSite, review_id: str) -> AsyncIterator[bytes]:
        if site.id in self.failing:
            raise FrigateUnavailableError(f"{site.id}: fake Frigate is off")
        if review_id not in self.clips:
            raise FrigateClipMissingError(f"{site.id}: {review_id}")
        for chunk in self.clips[review_id]:
            yield chunk

    async def aclose(self) -> None:
        pass


class FakeIndicatorPicker:
    """A fake re-check model: picks the first indicator whose keyword appears in the prompt."""

    def __init__(self, keywords: dict[str, str] | None = None) -> None:
        # keyword in the description → the indicator (verbatim from the rules) to pick
        self.keywords = keywords or {}
        self.prompts: list[str] = []
        self.failing = False
        self.delay_sec = 0.0

    async def pick(self, prompt: str, choices: list[str]) -> str:
        self.prompts.append(prompt)
        await asyncio.sleep(self.delay_sec)
        if self.failing:
            raise ChatUnavailableError("fake model is off")
        report = prompt.split("## Report", 1)[-1]
        for keyword, indicator in self.keywords.items():
            if keyword in report and indicator in choices:
                return indicator
        return choices[-1]

    async def aclose(self) -> None:
        pass
