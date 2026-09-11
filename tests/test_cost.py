"""Cost accounting and spend-cap tests — no API calls, no key needed.

Tailoring is the only part of this tool that spends money, and until the cap
existed the first sign of a bill was the bill. The important property here is
that the cap is checked BEFORE a call: a cap discovered by exceeding it is not
a cap.
"""

import pytest

from resume.cost import (
    CallCost,
    Ledger,
    cost_of,
    estimate_call,
    estimate_tokens,
    pricing_for,
    record_usage,
)


class FakeUsage:
    def __init__(self, input_tokens=0, output_tokens=0, **extra):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        for key, value in extra.items():
            setattr(self, key, value)


class FakeConfig:
    def __init__(self, values=None):
        self._values = values or {}

    def get(self, path, default=None):
        return self._values.get(path, default)


# -- pricing ---------------------------------------------------------------

def test_known_model_is_priced():
    prices = pricing_for("claude-opus-5")
    assert prices["input"] > 0 and prices["output"] > prices["input"]


def test_config_pricing_overrides_the_default():
    cfg = FakeConfig({"resume.pricing": {"claude-opus-5": {"input": 1.0, "output": 2.0}}})
    assert pricing_for("claude-opus-5", cfg) == {"input": 1.0, "output": 2.0}


def test_unknown_model_is_never_free():
    """Pricing an unknown model at zero would make every estimate say "free"
    and render the cap inert."""
    prices = pricing_for("some-future-model")
    assert prices["input"] > 0 and prices["output"] > 0


def test_cost_is_per_million_tokens():
    cfg = FakeConfig({"resume.pricing": {"m": {"input": 10.0, "output": 100.0}}})
    assert cost_of("m", 1_000_000, 0, cfg) == pytest.approx(10.0)
    assert cost_of("m", 0, 1_000_000, cfg) == pytest.approx(100.0)


# -- estimation ------------------------------------------------------------

def test_longer_prompts_estimate_higher():
    assert estimate_tokens("word " * 1000) > estimate_tokens("word " * 10)


def test_estimate_is_never_zero_for_real_text():
    assert estimate_call("claude-opus-5", "a job description") > 0


def test_estimate_scales_with_expected_output():
    prompt = "x" * 5000
    small = estimate_call("claude-opus-5", prompt, expected_output_tokens=100)
    large = estimate_call("claude-opus-5", prompt, expected_output_tokens=8000)
    assert large > small


# -- the ledger and the cap ------------------------------------------------

def test_spent_sums_the_calls():
    ledger = Ledger()
    ledger.record(CallCost("m", 1, 1, 0.25))
    ledger.record(CallCost("m", 1, 1, 0.75))
    assert ledger.spent == pytest.approx(1.0)


def test_no_cap_never_blocks():
    assert Ledger(cap_usd=0).would_exceed(1_000_000) is False


def test_cap_blocks_before_the_call_that_would_breach_it():
    """The whole point: the check happens before spending, not after."""
    ledger = Ledger(cap_usd=1.0)
    ledger.record(CallCost("m", 0, 0, 0.90))
    assert ledger.would_exceed(0.05) is False   # 0.95 total, still under
    assert ledger.would_exceed(0.20) is True    # 1.10 would breach


def test_cap_allows_a_call_landing_exactly_on_the_limit():
    ledger = Ledger(cap_usd=1.0)
    ledger.record(CallCost("m", 0, 0, 0.5))
    assert ledger.would_exceed(0.5) is False


def test_remaining_tracks_the_budget():
    ledger = Ledger(cap_usd=2.0)
    ledger.record(CallCost("m", 0, 0, 0.5))
    assert ledger.remaining == pytest.approx(1.5)


def test_remaining_is_none_without_a_cap():
    assert Ledger(cap_usd=0).remaining is None


def test_remaining_never_goes_negative():
    ledger = Ledger(cap_usd=1.0)
    ledger.record(CallCost("m", 0, 0, 5.0))
    assert ledger.remaining == 0.0


# -- recording real usage --------------------------------------------------

def test_usage_is_recorded_onto_the_ledger():
    ledger = Ledger()
    cfg = FakeConfig({"resume.pricing": {"m": {"input": 10.0, "output": 100.0}}})
    call = record_usage(ledger, "m", FakeUsage(1_000_000, 1_000_000), cfg)
    assert call.usd == pytest.approx(110.0)
    assert ledger.spent == pytest.approx(110.0)


def test_cache_tokens_are_counted_as_input():
    """Cached and cache-write tokens are billed; ignoring them under-reports."""
    ledger = Ledger()
    cfg = FakeConfig({"resume.pricing": {"m": {"input": 10.0, "output": 0.0}}})
    usage = FakeUsage(
        input_tokens=500_000,
        output_tokens=0,
        cache_creation_input_tokens=300_000,
        cache_read_input_tokens=200_000,
    )
    call = record_usage(ledger, "m", usage, cfg)
    assert call.input_tokens == 1_000_000
    assert call.usd == pytest.approx(10.0)


def test_missing_usage_fields_do_not_crash():
    ledger = Ledger()
    call = record_usage(ledger, "claude-opus-5", FakeUsage())
    assert call.input_tokens == 0 and call.usd == 0.0


def test_summary_reports_the_cap():
    ledger = Ledger(cap_usd=2.0)
    ledger.record(CallCost("m", 0, 0, 0.5))
    assert "$2.00 cap" in ledger.summary()


def test_summary_of_an_unused_ledger():
    assert "No API calls" in Ledger().summary()
