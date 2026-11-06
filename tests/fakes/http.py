import json
from typing import Any

import httpx


class RecordingTransport:
    """A MockTransport returning responses one by one and keeping the bodies sent."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            return self.responses.pop(0)

        return httpx.MockTransport(handle)
