from decimal import Decimal

from vigisift.domain.usage import LlmUsage, cost_usd


def test_cost_counts_cache_reads_at_a_tenth() -> None:
    usage = LlmUsage("claude-opus-5", 1_000_000, 0, cache_read_tokens=1_000_000)

    assert cost_usd(usage) == Decimal("5.5"), "$5 input + $0.5 cache read"


def test_an_unknown_model_costs_nothing_rather_than_crashing() -> None:
    assert cost_usd(LlmUsage("model-la", 10, 10)) == Decimal(0)
