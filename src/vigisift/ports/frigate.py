from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class FrigateSite:
    id: str
    api_url: str


class FrigateUnavailableError(Exception):
    """A site's Frigate API can't be read: network, sign-in, or a malformed response."""


class FrigateRejectedError(Exception):
    """Frigate rejected the change (invalid config, no such camera...)."""


class FrigateClipMissingError(Exception):
    """Frigate has no recording for this review: never recorded, or already deleted."""


class FrigateApi(Protocol):
    """Read data from each site's Frigate, to recover what MQTT missed.

    Returns raw data: the caller validates it, as with MQTT payloads.
    """

    async def reviews(
        self, site: FrigateSite, *, after: float, before: float, limit: int
    ) -> list[dict[str, Any]]:
        """Reviews touching (after, before), latest start first.

        In Frigate's terms: `start_time < before` and (not ended or
        `end_time > after`). Times are epoch seconds.
        """
        ...

    async def set_review_rules(
        self, site: FrigateSite, camera: str, *, prompt: str, concerns: list[str]
    ) -> None:
        """Set a camera's `review.genai.activity_context_prompt` and `additional_concerns`.
        Takes effect immediately with no restart, and is saved to the config."""
        ...

    async def review_rules(self, site: FrigateSite) -> dict[str, tuple[str, list[str]]]:
        """The prompt and concerns Frigate is USING for each camera (from the running config)."""
        ...

    def review_clip(self, site: FrigateSite, review_id: str) -> AsyncIterator[bytes]:
        """The review's video as MP4 chunks.

        Fails on the first chunk if there's no clip, so a caller can still answer with an
        error status: `FrigateClipMissingError` or `FrigateUnavailableError`.
        """
        ...

    async def aclose(self) -> None: ...
