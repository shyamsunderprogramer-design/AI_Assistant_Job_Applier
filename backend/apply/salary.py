"""The answer to "Desired salary?" for one job, by the person's own rule.

  * The posting gives a salary: ask for 80% to 95% of its maximum
    (posted $150,000 - $200,000 -> "$160,000 - $190,000"), never starting
    below the posted minimum ($200,000 - $215,000 -> "$200,000 - $204,000").
  * It gives none: the same band, taken from the market -- the typical
    maximum of similar postings in this database (same kind of role, same
    seniority, same pay period and currency), when there are enough of them.
  * Neither: None, and the profile's own desired salary (or the person) answers.

A form whose box takes only a number gets the middle of the band.
"""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from statistics import median

from backend.config.loader import PROJECT_ROOT

LOW, HIGH = 0.80, 0.95
MIN_SIMILAR = 8                        # fewer similar postings than this is not a market

FAMILIES = {
    "devops": ("devops", "dev ops"),
    "sre": ("site reliability", "sre"),
    "platform": ("platform",),
    "cloud": ("cloud",),
    "infrastructure": ("infrastructure", "infra "),
    "systems": ("systems engineer", "system engineer", "systems administrator"),
    "security": ("security", "devsecops"),
    "data": ("data engineer", "data platform"),
    "ml": ("machine learning", "ml ", "mlops", "ai "),
    "software": ("software engineer", "developer", "backend", "full stack"),
    "network": ("network",),
    "build": ("build", "release", "ci/cd"),
}
LEVELS = (("principal", ("principal", "distinguished")), ("staff", ("staff",)), ("lead", ("lead",)),
          ("manager", ("manager", "director", "head of")), ("senior", ("senior", "sr.", "sr ", " iii", " iv")),
          ("junior", ("junior", "jr", "associate", "entry", "intern")))


def family(title: str) -> set[str]:
    t = f" {(title or '').lower()} "
    return {name for name, words in FAMILIES.items() if any(w in t for w in words)}


def level(title: str) -> str:
    t = f" {(title or '').lower()} "
    return next((name for name, words in LEVELS if any(w in t for w in words)), "mid")


@lru_cache(maxsize=1)
def _postings() -> tuple:
    """Every posted maximum, with what is needed to compare it. Read once per process."""
    path = PROJECT_ROOT / "data" / "jobs.db"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT title, salary_max, salary_period, COALESCE(salary_currency, 'USD') FROM jobs "
                          "WHERE salary_max IS NOT NULL AND salary_max > 0").fetchall()
    return tuple((family(t), level(t), m, p or "year", c) for t, m, p, c in rows)


def market_max(title: str, period: str = "year", currency: str = "USD", rows=None) -> tuple[int, int] | None:
    """(typical maximum, how many postings it is from) for roles like this one, or None."""
    fam, lvl = family(title), level(title)
    if not fam:
        return None
    rows = _postings() if rows is None else rows
    alike = [m for f, l, m, p, c in rows if f & fam and l == lvl and p == period and c == currency]
    if len(alike) < MIN_SIMILAR:
        return None
    return int(median(alike)), len(alike)


def band(job, rows=None) -> dict | None:
    """{low, high, period, currency, basis} for this job, or None when there is nothing to go on."""
    period = getattr(job, "salary_period", None) or "year"
    currency = getattr(job, "salary_currency", None) or "USD"
    top = getattr(job, "salary_max", None) or getattr(job, "salary_min", None)
    basis = "posted"
    if not top:
        found = market_max(getattr(job, "title", ""), period, currency, rows)
        if not found:
            return None
        top, similar = found
        basis = f"market: {similar} similar postings"
    step = 1 if period == "hour" else 1000
    low, high = round(top * LOW / step) * step, round(top * HIGH / step) * step
    floor = getattr(job, "salary_min", None) if basis == "posted" else None
    if floor and low < floor:
        # Never ask for less than the company itself posted ($200k-$215k asked $172k).
        low, high = int(floor), max(high, int(floor))
    return {"low": low, "high": high, "period": period, "currency": currency, "basis": basis}


def _money(n: int, currency: str) -> str:
    sign = {"USD": "$", "CAD": "CA$", "EUR": "€", "GBP": "£"}.get(currency, currency + " ")
    return f"{sign}{n:,}"


def answer(job, rows=None) -> str | None:
    """The band as a form answer: "$160,000 – $190,000" (per hour when the posting pays hourly)."""
    b = band(job, rows)
    if b is None:
        return None
    text = (_money(b['low'], b['currency']) if b['low'] == b['high']
            else f"{_money(b['low'], b['currency'])} – {_money(b['high'], b['currency'])}")
    return text + (" per hour" if b["period"] == "hour" else "")


def single_figure(text: str) -> str | None:
    """The middle of a range, digits only, for a box that takes a number: "$160,000 – $190,000" -> "175000"."""
    numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text or "")]
    if not numbers:
        return None
    return str(round(sum(numbers[:2]) / len(numbers[:2])))


def is_salary_question(label: str) -> bool:
    text = (label or "").lower()
    return ("salary" in text or "compensation" in text or "pay expectation" in text) and \
        any(w in text for w in ("desired", "expect", "requirement", "looking for", "target")) and "current" not in text


def job_by_id(job_id: int):
    """The fields this module reads, for one job, straight from the database (read-only)."""
    from types import SimpleNamespace
    path = PROJECT_ROOT / "data" / "jobs.db"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        row = db.execute("SELECT title, salary_min, salary_max, salary_period, salary_currency, company "
                         "FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    return SimpleNamespace(title=row[0], salary_min=row[1], salary_max=row[2], salary_period=row[3],
                           salary_currency=row[4], company=row[5], id=job_id)
