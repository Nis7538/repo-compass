"""Prices and cost arithmetic for the review agent's cost cap."""

import pytest

from compass.agent.pricing import PRICES, UnknownModelError, Usage, cost, price_of


def test_cost_uses_each_rate():
    usage = Usage(
        input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # Opus 5.5: $4 input + $5 cache write + $0.20 cache read + $20 output.
    assert cost(usage, price_of("claude-opus-5-5")) == pytest.approx(29.20)
    assert cost(Usage(), price_of("claude-opus-5-5")) == 0


def test_cache_writes_cost_more_than_input_and_reads_less():
    for price in PRICES.values():
        assert price.cache_write == pytest.approx(price.input * 1.25)
        assert price.cache_read < price.input < price.output


def test_unknown_model_is_refused_with_what_to_do():
    with pytest.raises(UnknownModelError, match="cost cap cannot be enforced") as err:
        price_of("claude-imaginary-9")
    assert "claude-opus-5-5" in str(err.value)
    assert "pricing.py" in str(err.value)


def test_usage_adds_up_and_counts_every_prompt_token():
    total = Usage()
    total.add(Usage(10, 20, 30, 5))
    total.add(Usage(1, 2, 3, 4))
    assert total == Usage(11, 22, 33, 9)
    assert total.prompt_tokens == 66
