"""Pure, testable Anthropic API pricing lookup and cost calculation for
cycle-usage reporting. No CrewAI or STATE_DIR dependency, same philosophy
as scoring.py - the arithmetic should be verifiable in isolation from the
agent runtime.

Rates are USD per million tokens (MTok), confirmed against the exact table
supplied for this repo - never substitute recalled/assumed pricing here,
this is a fast-moving part of the API and Sonnet 5's price steps up on a
known future date.
"""
from datetime import date

SONNET_5_PRICE_STEP_DATE = date(2026, 9, 1)

PRICING_TABLE = {
    "claude-haiku-4-5": {
        "base_input": 1.0, "cache_write_5m": 1.25, "cache_write_1h": 2.0,
        "cache_hit": 0.10, "output": 5.0,
    },
    "claude-sonnet-5": {
        "before_step": {
            "base_input": 2.0, "cache_write_5m": 2.50, "cache_write_1h": 4.0,
            "cache_hit": 0.20, "output": 10.0,
        },
        "from_step": {
            "base_input": 3.0, "cache_write_5m": 3.75, "cache_write_1h": 6.0,
            "cache_hit": 0.30, "output": 15.0,
        },
    },
}


def get_pricing(model: str, as_of: date) -> dict:
    """Return the {base_input, cache_write_5m, cache_write_1h, cache_hit,
    output} rate dict (USD per MTok) for `model`, using the rate in effect
    on `as_of` - Sonnet 5's price steps up on SONNET_5_PRICE_STEP_DATE, so
    this must compare against the cycle's own date, not wall-clock-at-
    code-time, so the report stays correct on both sides of that date
    without a redeploy. Raises ValueError for an unknown model rather than
    guessing a rate.
    """
    if model == "claude-haiku-4-5":
        return dict(PRICING_TABLE["claude-haiku-4-5"])
    if model == "claude-sonnet-5":
        tier = "from_step" if as_of >= SONNET_5_PRICE_STEP_DATE else "before_step"
        return dict(PRICING_TABLE["claude-sonnet-5"][tier])
    raise ValueError(f"no pricing known for model '{model}'")


def compute_cycle_cost(
    model: str,
    as_of: date,
    base_input_tokens: int,
    cache_write_tokens: int,
    cache_hit_tokens: int,
    completion_tokens: int,
    cache_write_ttl: str = "5m",
) -> float:
    """USD cost for one cycle's token usage, given the already-separated
    base-input / cache-write / cache-hit(read) / completion token counts
    (as reported by crewai's usage_metrics - these are non-overlapping
    categories, never summed with each other before this call).

    cache_write_ttl must be "5m" or "1h". This repo's crewai Anthropic
    provider stamps cache_control without an explicit ttl
    (crewai.llms.providers.anthropic.completion._stamp_cache_control_on_
    message: `{"type": "ephemeral"}`, no "ttl" key) - verified directly in
    the installed package, not assumed - which is Anthropic's 5-minute
    tier by default, not the 1-hour one. Only pass "1h" if that provider
    behavior is later confirmed to have changed.
    """
    if cache_write_ttl not in ("5m", "1h"):
        raise ValueError(f"cache_write_ttl must be '5m' or '1h', got '{cache_write_ttl}'")

    rates = get_pricing(model, as_of)
    write_rate = rates["cache_write_5m"] if cache_write_ttl == "5m" else rates["cache_write_1h"]
    cost = (
        base_input_tokens * rates["base_input"]
        + cache_write_tokens * write_rate
        + cache_hit_tokens * rates["cache_hit"]
        + completion_tokens * rates["output"]
    ) / 1_000_000
    return round(cost, 6)
