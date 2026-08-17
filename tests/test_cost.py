"""
Tests for the per-turn cost arithmetic.

Pure numbers: no client, no graph, no state. The figures below are derived from
`MODEL_PRICING` rather than written out, so a price correction does not turn into a
test failure - what is pinned is the arithmetic and the fallback rule, not the rates.
"""

import pytest
from anthropic.types import Usage

from app.agents.cost import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    MODEL_PRICING,
    PER_MILLION,
    _rate,
    turn_cost_usd,
)
from app.config import settings

MODEL = "claude-sonnet-4-6"


def test_a_million_tokens_each_way_costs_the_published_rates():
    input_rate, output_rate = MODEL_PRICING[MODEL]

    cost = turn_cost_usd(MODEL, Usage(input_tokens=PER_MILLION, output_tokens=PER_MILLION))

    assert cost == pytest.approx(input_rate + output_rate)


def test_input_and_output_are_priced_differently():
    """Output is the expensive half; a function that averaged them would pass everything else."""
    same = 100_000
    input_rate, output_rate = MODEL_PRICING[MODEL]

    only_input = turn_cost_usd(MODEL, Usage(input_tokens=same, output_tokens=0))
    only_output = turn_cost_usd(MODEL, Usage(input_tokens=0, output_tokens=same))

    assert only_input == pytest.approx(same * input_rate / PER_MILLION)
    assert only_output == pytest.approx(same * output_rate / PER_MILLION)
    assert only_output > only_input


def test_a_turn_that_used_nothing_costs_nothing():
    assert turn_cost_usd(MODEL, Usage(input_tokens=0, output_tokens=0)) == 0.0


def test_a_response_without_usage_is_charged_zero_rather_than_raising():
    """Cost accounting must never be the thing that breaks a turn."""
    assert turn_cost_usd(MODEL, None) == 0.0


def test_cache_tokens_are_priced_at_their_multipliers_of_the_input_rate():
    """
    Zero on every response today - nothing here sets cache_control - and priced anyway,
    so that adding prompt caching does not silently start under-reporting spend.
    """
    input_rate, _ = MODEL_PRICING[MODEL]

    written = turn_cost_usd(
        MODEL,
        Usage(input_tokens=0, output_tokens=0, cache_creation_input_tokens=PER_MILLION),
    )
    read = turn_cost_usd(
        MODEL,
        Usage(input_tokens=0, output_tokens=0, cache_read_input_tokens=PER_MILLION),
    )

    assert written == pytest.approx(input_rate * CACHE_WRITE_MULTIPLIER)
    assert read == pytest.approx(input_rate * CACHE_READ_MULTIPLIER)
    # Reading a cached prefix is the cheap case; writing it costs more than not caching.
    assert read < input_rate < written


def test_absent_cache_counters_are_not_confused_with_zero():
    """They come back as None, and None does not multiply."""
    usage = Usage(input_tokens=10, output_tokens=10)

    assert usage.cache_read_input_tokens is None
    assert turn_cost_usd(MODEL, usage) > 0


def test_an_unknown_model_is_priced_at_the_most_expensive_one_known():
    """
    A guardrail that fails open is not a guardrail: pricing an unrecognized CLAUDE_MODEL
    at zero would disable the cost cap silently, which is the failure worth preventing.
    """
    dearest = max(MODEL_PRICING.values(), key=lambda rate: rate[1])

    assert _rate("some-model-released-next-year") == dearest
    assert _rate("some-model-released-next-year") >= _rate(MODEL)


def test_the_configured_model_has_a_price_of_its_own():
    """
    Not a law - the fallback exists precisely so an unlisted model still works - but the
    model this deployment actually runs should not be paying the fallback's estimate.
    """
    assert settings.claude_model in MODEL_PRICING
