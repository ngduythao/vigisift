from dataclasses import dataclass
from decimal import Decimal

# USD per 1 million tokens, Anthropic's direct API price. Cache writes (5-minute TTL)
# cost 1.25 times the input price, cache reads 0.1 times. Change prices here.
PRICES_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}
_CACHE_WRITE = Decimal("1.25")
_CACHE_READ = Decimal("0.1")
_MTOK = Decimal(1_000_000)


@dataclass(frozen=True)
class LlmUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def __add__(self, other: "LlmUsage") -> "LlmUsage":
        return LlmUsage(
            model=self.model if self.model == other.model else f"{self.model}+{other.model}",
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


def cost_usd(usage: LlmUsage) -> Decimal:
    """One call's cost. An unknown model costs 0: tokens stay right, only the price is missing."""
    price = PRICES_PER_MTOK.get(usage.model)
    if price is None:
        return Decimal(0)
    input_price, output_price = price
    billed_input = (
        usage.input_tokens
        + usage.cache_write_tokens * _CACHE_WRITE
        + usage.cache_read_tokens * _CACHE_READ
    )
    return (billed_input * input_price + usage.output_tokens * output_price) / _MTOK
