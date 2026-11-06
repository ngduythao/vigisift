from typing import Any

from vigisift.domain.usage import LlmUsage


def usage_from_anthropic(model: str, usage: Any) -> LlmUsage:
    """Read `response.usage` from the Anthropic SDK. Cache fields can be None."""
    return LlmUsage(
        model=model,
        input_tokens=int(usage.input_tokens or 0),
        output_tokens=int(usage.output_tokens or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )
