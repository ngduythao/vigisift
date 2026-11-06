import json
from typing import Any

import httpx

from vigisift.ports.llm import ChatUnavailableError


class OllamaIndicatorPicker:
    """Ask Ollama to pick one choice, constrained by a JSON schema `enum`.

    Ollama's grammar blocks answers outside the list, so no guessing from prose;
    the answer is still checked because it's external data.
    """

    def __init__(
        self,
        model: str,
        host: str,
        *,
        num_ctx: int,
        keep_alive: str = "10m",
        timeout_sec: float = 60,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._num_ctx = num_ctx
        self._keep_alive = keep_alive
        self._http = httpx.AsyncClient(base_url=host, timeout=timeout_sec, transport=transport)

    async def pick(self, prompt: str, choices: list[str]) -> str:
        body: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "format": {
                "type": "object",
                "properties": {"indicator": {"type": "string", "enum": choices}},
                "required": ["indicator"],
            },
            "think": False,
            "stream": False,
            "keep_alive": self._keep_alive,
            # num_ctx must match what Frigate sends for the same model: otherwise
            # Ollama reloads the model, costing tens of seconds each time.
            "options": {"temperature": 0, "num_ctx": self._num_ctx, "num_predict": 200},
        }
        try:
            response = await self._http.post("/api/generate", json=body)
            response.raise_for_status()
            answer = json.loads(response.json()["response"])["indicator"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            raise ChatUnavailableError(f"ollama: {type(error).__name__}") from error
        if answer not in choices:
            raise ChatUnavailableError("ollama: answer is not one of the choices")
        return str(answer)

    async def aclose(self) -> None:
        await self._http.aclose()
