"""
What a turn cost, in dollars.

Split out of `app/agents/graph.py` because it is the one part of the budget that is
arithmetic rather than orchestration: the graph owns *when* to stop, this module owns
*how much has been spent*. It touches nothing and can be tested on numbers alone.

## The prices are a copy, and copies go stale

`MODEL_PRICING` is USD per million tokens, transcribed from Anthropic's published rates.
Nothing here can verify them at runtime - the Messages API bills the account, it does not
return a price - so the figures below are a local estimate of spend and not an invoice.
Two consequences worth stating rather than discovering:

- Changing `settings.claude_model` to a model missing from the table is a configuration
  change that silently alters the cap's meaning. See `_rate` for what happens then.
- Introductory or negotiated pricing is not modelled. The cap is a bound on estimated
  list-price spend, which is the useful thing to bound in a demo, not an accounting figure.

## Cache tokens

Nothing in this project sets `cache_control`, so the cache counters are zero on every
response today. They are priced anyway - at the published multipliers of the input rate -
because the alternative is a cost function that silently under-reports the day prompt
caching is added, which is exactly the day the numbers start mattering.
"""

from anthropic.types import Usage

# USD per million tokens, as (input, output).
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Multipliers on the *input* rate, per Anthropic's published cache pricing.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1

PER_MILLION = 1_000_000


def _rate(model: str) -> tuple[float, float]:
    """
    The (input, output) rate for a model, falling back to the most expensive one known.

    A guardrail that fails open is not a guardrail: pricing an unknown model at zero would
    let a misconfigured `CLAUDE_MODEL` disable the cost cap entirely, and quietly. Erring
    upwards instead means an unrecognized model stops a conversation sooner than it
    strictly had to, which is the survivable direction for a spending limit to be wrong in.
    """
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]

    return max(MODEL_PRICING.values(), key=lambda rate: rate[1])


def turn_cost_usd(model: str, usage: Usage | None) -> float:
    """
    What one `messages.create` cost, from the usage the response reports.

    `usage` is optional because the cost accounting must never be the thing that breaks a
    turn: a response without it is charged zero rather than raising. The cache counters are
    `None` on a response that used no caching, which is every response here so far.
    """
    if usage is None:
        return 0.0

    input_rate, output_rate = _rate(model)

    tokens = (
        usage.input_tokens * input_rate
        + usage.output_tokens * output_rate
        + (usage.cache_creation_input_tokens or 0) * input_rate * CACHE_WRITE_MULTIPLIER
        + (usage.cache_read_input_tokens or 0) * input_rate * CACHE_READ_MULTIPLIER
    )

    return tokens / PER_MILLION
