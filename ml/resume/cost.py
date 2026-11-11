"""Token accounting and the spend ceiling for tailoring runs.

Tailoring is the only part of this tool that costs money, and until now it had
no ceiling at all: `tailor --limit 80` would have made 80 Opus calls and the
first sign of the bill would have been the bill. The only brake was
`resume.overwrite: false` skipping jobs that already had output.

Three things close that gap:

  * an ESTIMATE before a run, from the real token counts of the actual prompts
  * a CAP checked before each call, so the run stops *before* exceeding it
  * a LEDGER of what each call actually cost, so the estimate can be checked
    against reality rather than trusted

Prices are per million tokens and are a snapshot — they are config, not
constants, because a stale hardcoded price silently produces a wrong estimate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# USD per million tokens. Override in config.yaml under `resume.pricing`.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
}

# Rough characters-per-token for English prose. Only used for the pre-run
# estimate, where the prompts are known but have not been sent yet.
CHARS_PER_TOKEN = 3.7


@dataclass
class CallCost:
    """What one API call actually cost."""

    model: str
    input_tokens: int
    output_tokens: int
    usd: float

    def __str__(self) -> str:  # pragma: no cover - display helper
        return (
            f"{self.input_tokens:,} in + {self.output_tokens:,} out = ${self.usd:.4f}"
        )


@dataclass
class Ledger:
    """Running total for one tailoring run, and the cap it must respect."""

    cap_usd: float = 0.0  # 0 = no cap
    calls: list[CallCost] = field(default_factory=list)

    @property
    def spent(self) -> float:
        return sum(call.usd for call in self.calls)

    @property
    def remaining(self) -> float | None:
        return None if self.cap_usd <= 0 else max(0.0, self.cap_usd - self.spent)

    def would_exceed(self, projected_usd: float) -> bool:
        """True if one more call of this size would breach the cap.

        Checked BEFORE the call. Checking afterwards would mean the cap is
        discovered by exceeding it, which is not a cap.
        """
        if self.cap_usd <= 0:
            return False
        return self.spent + projected_usd > self.cap_usd

    def record(self, call: CallCost) -> CallCost:
        self.calls.append(call)
        return call

    def summary(self) -> str:
        if not self.calls:
            return "No API calls made."
        line = f"{len(self.calls)} call(s), ${self.spent:.4f} total"
        if self.cap_usd > 0:
            line += f" of a ${self.cap_usd:.2f} cap"
        return line


def pricing_for(model: str, cfg=None) -> dict[str, float]:
    """Per-million-token prices for a model, config first."""
    configured = (cfg.get("resume.pricing", {}) if cfg else None) or {}
    if model in configured:
        return {
            "input": float(configured[model].get("input", 0.0)),
            "output": float(configured[model].get("output", 0.0)),
        }
    if model in DEFAULT_PRICING:
        return DEFAULT_PRICING[model]
    # An unknown model must not silently price at zero — that would make every
    # estimate say "free" and render the cap inert.
    log.warning("No pricing known for %s; treating it as the most expensive model.", model)
    return max(DEFAULT_PRICING.values(), key=lambda p: p["output"])


def cost_of(model: str, input_tokens: int, output_tokens: int, cfg=None) -> float:
    prices = pricing_for(model, cfg)
    return (
        input_tokens / 1_000_000 * prices["input"]
        + output_tokens / 1_000_000 * prices["output"]
    )


def estimate_tokens(text: str) -> int:
    """Approximate token count for text that has not been sent yet."""
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def estimate_call(
    model: str, prompt_text: str, expected_output_tokens: int = 4000, cfg=None
) -> float:
    """Projected USD for one tailoring call.

    `expected_output_tokens` defaults high rather than low: an estimate that
    under-promises is the one that lets a run blow through its cap.
    """
    return cost_of(model, estimate_tokens(prompt_text), expected_output_tokens, cfg)


def record_usage(ledger: Ledger, model: str, usage, cfg=None) -> CallCost:
    """Record real usage from an API response onto the ledger."""
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    # Cached and cache-write tokens are billed differently; count them as input
    # so the ledger never under-reports.
    input_tokens += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    input_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    return ledger.record(
        CallCost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd=cost_of(model, input_tokens, output_tokens, cfg),
        )
    )


class SpendCapReached(Exception):
    """Raised before a call that would breach the configured cap."""


# -- a hard limit for a whole preparation (Track A1, Nov 2026) -----------------------------------------
#
# A preparation calls the writer in several places -- the posting's requirements, the evidence match,
# the rewrite, the recruiter's read -- and only the rewrite was ever costed. While a preparation holds a
# budget, every call to a writer that bills (ml/resume/llm.complete) is priced before it is made and
# refused if it would pass the cap, and its real usage is recorded after. Process-wide, not per thread:
# some of those calls run in threads of their own, and a limit a thread could step around is no limit.
# With two budgets open in one process every billed call counts against both, which can only stop a run
# early, never let one through.

import threading as _threading
from contextlib import contextmanager as _contextmanager

_ACTIVE: list[Ledger] = []
_ACTIVE_LOCK = _threading.Lock()


@_contextmanager
def budget(ledger: Ledger):
    with _ACTIVE_LOCK:
        _ACTIVE.append(ledger)
    try:
        yield ledger
    finally:
        with _ACTIVE_LOCK:
            if ledger in _ACTIVE:
                _ACTIVE.remove(ledger)
            _RESERVED.pop(id(ledger), None)


def active_budgets() -> list[Ledger]:
    with _ACTIVE_LOCK:
        return list(_ACTIVE)


_RESERVED: dict[int, float] = {}        # id(ledger) -> the price of billed calls under way, held against its cap


def check_billed_call(model: str, prompt_text: str, expected_output_tokens: int, cfg=None) -> float:
    """Before a billed call: refuse it if any open budget would pass its cap counting what is spent AND what calls
    under way may still spend; else hold its price against every open budget until `settle_billed_call`.
    Priced at the call's full allowed output (A1.1: priced at half of it in A1). Returns the price held."""
    projected = estimate_call(model, prompt_text, expected_output_tokens, cfg)
    with _ACTIVE_LOCK:
        for ledger in _ACTIVE:
            held = _RESERVED.get(id(ledger), 0.0)
            if ledger.cap_usd <= 0 or ledger.spent + held + projected > ledger.cap_usd:
                raise SpendCapReached(
                    f"The spending limit (${ledger.cap_usd:.2f}) would be passed by the next call (up to "
                    f"${projected:.3f}; ${ledger.spent:.3f} spent and ${held:.3f} under way), so it was not made.")
        holders = list(_ACTIVE)
        for ledger in holders:
            _RESERVED[id(ledger)] = _RESERVED.get(id(ledger), 0.0) + projected
    return _Hold(projected, holders)


class _Hold(float):
    """A held price, and the budgets that hold it -- settled against those, not whichever are open later."""

    def __new__(cls, value: float, holders: list):
        made = super().__new__(cls, value)
        made.holders = holders
        return made


def settle_billed_call(model: str, usage, held: float, cfg=None) -> None:
    """After a billed call: its held price is let go by the budgets that held it, and its real cost recorded --
    or, when no usage came back (it failed after it may have been billed), its full price, so failures still
    count towards the limit (A1.1 review)."""
    holders = getattr(held, "holders", None)
    with _ACTIVE_LOCK:
        ledgers = list(holders) if holders is not None else list(_ACTIVE)
        for ledger in ledgers:
            _RESERVED[id(ledger)] = max(0.0, _RESERVED.get(id(ledger), 0.0) - float(held))
    for ledger in ledgers:
        if usage is not None:
            record_usage(ledger, model, usage, cfg)
        else:
            ledger.record(CallCost(model=model, input_tokens=0, output_tokens=0, usd=float(held)))


def record_billed_call(model: str, usage, cfg=None) -> None:
    for ledger in active_budgets():
        record_usage(ledger, model, usage, cfg)
