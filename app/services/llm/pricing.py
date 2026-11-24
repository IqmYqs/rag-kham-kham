"""Cost estimate from token usage (US-F02). Empty pricing table = free tier = 0."""

from decimal import Decimal
from typing import Any

_PER = Decimal(1_000_000)


def estimate_cost(
    pricing: dict[str, Any], model: str, input_tokens: int, output_tokens: int
) -> Decimal:
    """`pricing` maps model name -> {input_per_m: USD, output_per_m: USD}."""
    price = pricing.get(model)
    if not price:
        return Decimal(0)
    cost = (
        Decimal(str(price.get("input_per_m", 0))) * input_tokens
        + Decimal(str(price.get("output_per_m", 0))) * output_tokens
    ) / _PER
    return cost.quantize(Decimal("0.000001"))
