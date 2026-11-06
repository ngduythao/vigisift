from typing import Protocol


class IndicatorPicker(Protocol):
    """The way out to the text re-check model: pick exactly ONE of the given choices.

    Raises `ChatUnavailableError` (ports.llm) when the model can't answer, so the
    caller leaves the alert and tries on the next pass.
    """

    async def pick(self, prompt: str, choices: list[str]) -> str: ...

    async def aclose(self) -> None: ...
