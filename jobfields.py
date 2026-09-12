"""Facts a posting states about itself: workplace, experience, pay.

All three are already in text the scrape stores, so nothing here touches the
network. What each function refuses to answer matters as much as what it
returns — a confident wrong salary is worse than a blank one, because a blank
prompts you to go and look.

None means "the posting did not say". Callers render that; it is never stored
as words, or every filter would have to parse English back out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# HTML becomes text carrying &nbsp; as U+00A0, and a pattern written with an
# ordinary space silently stops matching. One posting's "With\xa0nearly 20
# years" slipped every company-history guard because of exactly this.
_UNICODE_SPACE = re.compile(r"[\u00a0\u2007\u202f\u2009\u200a\u2002-\u2006]")


def _normalise(text: str | None) -> str:
    """Ordinary spaces, so a pattern written with one keeps working."""
    return _UNICODE_SPACE.sub(" ", text) if text else ""


REMOTE = "remote"
HYBRID = "hybrid"
ONSITE = "onsite"

# Any of the dashes a board might use, or the word.
_RANGE = r"(?:\s*(?:-|–|—|~|\bto\b)\s*)"
# The symbol may sit before either number, so both sides must allow one.
_MONEY = r"[$£€]?\s*([\d][\d,]*(?:\.\d+)?)\s*([KkMm])?"

# An annual salary outside this is not a salary: below it lies a 401(k) match
# or an hourly figure, above it a funding round.
_MIN_PLAUSIBLE = 10_000
_MAX_PLAUSIBLE = 2_000_000

_PAY_CONTEXT = re.compile(
    r"\b(salary|compensation|base pay|pay range|pay band|annual|annually|"
    r"per year|/\s*yr|OTE|total target)\b",
    re.I,
)
# Greenhouse and Ashby both stamp the currency on, which is unambiguous enough
# to trust without any surrounding words.
_CURRENCY_SUFFIX = re.compile(r"\b(USD|CAD|GBP|EUR|AUD)\b", re.I)

_HOURLY = re.compile(r"\b(per hour|hourly|/\s*hr|an hour)\b", re.I)


@dataclass(frozen=True)
class Salary:
    low: int
    high: int
    currency: str = "USD"
    period: str = "year"

    def label(self) -> str:
        # A bare "$" on a Canadian or Australian range reads as US dollars,
        # which is a 30% lie. Name any currency that is not USD.
        mark = {"USD": "$", "CAD": "C$", "AUD": "A$", "GBP": "£", "EUR": "€"}.get(
            self.currency, f"{self.currency} "
        )
        if self.period == "hour":
            return f"{mark}{self.low:,}–{mark}{self.high:,}/hr"
        return f"{mark}{round(self.low / 1000)}K–{mark}{round(self.high / 1000)}K"


def _to_number(raw: str, suffix: str | None) -> float | None:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    if suffix and suffix.lower() == "k":
        value *= 1_000
    elif suffix and suffix.lower() == "m":
        value *= 1_000_000
    return value


def salary(text: str | None) -> Salary | None:
    """The pay range a posting publishes, or None when it publishes none.

    Four formats appear in real boards and all four are handled:
        $285,000—$325,000 USD        $140.8K – $231K • Offers Equity
        $180,000 - $240,000 annually between $135,000 to $145,000
    """
    text = _normalise(text)
    if not text:
        return None

    pattern = re.compile(_MONEY + _RANGE + _MONEY)
    for match in pattern.finditer(text):
        low = _to_number(match.group(1), match.group(2))
        high = _to_number(match.group(3), match.group(4))
        if low is None or high is None or low > high:
            continue

        window = text[max(0, match.start() - 140): match.end() + 60]
        hourly = bool(_HOURLY.search(window))
        if hourly:
            # An hourly rate is real pay, just on a different clock.
            if not (5 <= low <= 500 and 5 <= high <= 500):
                continue
            return Salary(int(low), int(high), _currency(window), "hour")

        if not (_MIN_PLAUSIBLE <= low <= _MAX_PLAUSIBLE):
            continue
        if not (_MIN_PLAUSIBLE <= high <= _MAX_PLAUSIBLE):
            continue
        # Three ways to know this is money: the board stamped a currency code
        # on it, a symbol is attached to the numbers themselves, or something
        # nearby calls it pay. Without one, it is just a pair of numbers.
        stamped = _CURRENCY_SUFFIX.search(window)
        symbol = any(mark in match.group(0) for mark in ("$", "£", "€"))
        if not (stamped or symbol or _PAY_CONTEXT.search(window)):
            continue
        return Salary(int(low), int(high), _currency(window), "year")
    return None


def _currency(window: str) -> str:
    found = _CURRENCY_SUFFIX.search(window)
    if found:
        return found.group(1).upper()
    if "£" in window:
        return "GBP"
    if "€" in window:
        return "EUR"
    return "USD"


# Years must sit next to something that makes them a requirement; a company
# "celebrating 10 years" is not asking for a decade of experience.
_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?)?\s*(?:\+\s*)?years?\b",
    re.I,
)
# How a company talks about ITSELF. "With more than 40 years of experience in
# the industry" satisfies every positive cue and is not a requirement at all.
_COMPANY_HISTORY = re.compile(
    r"\b(?:with (?:more than|over|nearly|almost)|for (?:more than|over|nearly)|"
    r"we(?:'ve| have) been|founded|established|celebrating|history of|legacy of|"
    r"combined|collectively|team (?:has|with)|our (?:team|founders|leadership))\b",
    re.I,
)
_EXPERIENCE_CONTEXT = re.compile(
    r"\b(experience|experienced|background|working|work|building|managing|"
    r"developing|engineering|professional|industry|hands[- ]on|expertise|"
    r"practice|career|minimum|at least|required|proven)\b",
    re.I,
)


def experience_years(text: str | None) -> tuple[int | None, int | None]:
    """(minimum, maximum) years a posting asks for; (None, None) if unstated.

    "5+ years" is (5, None). "3-5 years" is (3, 5).

    A posting states several bars — "4+ years in DevOps" and "2+ years with
    Kubernetes" — and you must clear all of them, so the binding requirement is
    the HIGHEST of the stated minimums, not the lowest. Reporting the lowest
    made a role wanting four years look like it wanted two.
    """
    text = _normalise(text)
    if not text:
        return None, None

    best_low: int | None = None
    best_high: int | None = None
    for match in _YEARS.finditer(text):
        window = text[max(0, match.start() - 90): match.end() + 90]
        if not _EXPERIENCE_CONTEXT.search(window):
            continue
        # Look only at what comes BEFORE: "With more than 40 years" is the
        # company describing itself, and the giveaway is always the lead-in.
        if _COMPANY_HISTORY.search(text[max(0, match.start() - 60): match.start()]):
            continue
        low = int(match.group(1))
        high = int(match.group(2)) if match.group(2) else None
        # Nobody requires a quarter-century; past this it is prose about
        # something else that slipped the other guards.
        if not (0 < low <= 25):
            continue
        if high is not None and not (low <= high <= 40):
            high = None
        if best_low is None or low > best_low:
            best_low, best_high = low, high
    return best_low, best_high


# "Remote-friendly" and "remote-first" describe a company's culture, not where
# this job is done, so they must not read as remote.
_NOT_REALLY_REMOTE = re.compile(r"remote[- ](?:friendly|first|ish)", re.I)
_REMOTE = re.compile(r"\bremote\b|\banywhere\b|\bdistributed\b|\bwork from home\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b|\bflexible\b", re.I)
_ONSITE = re.compile(r"\bon-?site\b|\bin[- ]office\b|\bin[- ]person\b", re.I)
# A place needs more than a country to mean "come to this office".
_A_PLACE = re.compile(r"[A-Za-z]+\s*,\s*[A-Za-z]", re.I)


def workplace(
    location: str | None,
    title: str | None = None,
    *,
    is_remote: bool | None = None,
    workplace_type: str | None = None,
) -> str | None:
    """remote / hybrid / onsite, or None when the posting does not make it clear.

    Read from the LOCATION, never the description: descriptions are full of
    "we're a remote-first company" boilerplate that says nothing about the job.
    Ashby publishes this as structured fields, so prefer those when given.
    """
    if workplace_type:
        known = workplace_type.strip().lower()
        if "hybrid" in known:
            return HYBRID
        if "remote" in known:
            return REMOTE
        if known in ("onsite", "on-site", "on site", "in office", "in-office"):
            return ONSITE
    if is_remote is True:
        return REMOTE

    text = _normalise(" ".join(part for part in (location, title) if part)).strip()
    if not text:
        return None

    hybrid = bool(_HYBRID.search(text))
    onsite = bool(_ONSITE.search(text))
    cleaned = _NOT_REALLY_REMOTE.sub(" ", text)
    remote = bool(_REMOTE.search(cleaned))

    # A posting listing several arrangements is offering a choice, and hybrid
    # is the honest summary of "some days here, some days not".
    if hybrid or (remote and onsite):
        return HYBRID
    if remote:
        return REMOTE
    if onsite or _A_PLACE.search(text):
        return ONSITE
    # A bare country is not an office. Absence of "remote" is not evidence.
    return None


# -- rendering --------------------------------------------------------------
# Blank would be ambiguous between "the posting was silent" and "nobody has
# looked yet". These say which.

UNSTATED = "not published"


def salary_label(low: int | None, high: int | None,
                 currency: str | None = "USD", period: str | None = "year") -> str:
    if low is None or high is None:
        return UNSTATED
    return Salary(low, high, currency or "USD", period or "year").label()


def experience_label(low: int | None, high: int | None) -> str:
    if low is None:
        return UNSTATED
    if high:
        return f"{low}–{high} yrs"
    return f"{low}+ yrs"


def workplace_label(value: str | None) -> str:
    return value or UNSTATED


def derive(title: str | None, location: str | None,
           description: str | None, requirements: str | None) -> dict[str, object]:
    """Every derived field for one posting, as column -> value.

    One function so the scrape and the backfill cannot drift: a parser fix has
    to reach both, and two copies of this logic would eventually disagree about
    the same posting.
    """
    # Requirements first — that is where a posting states its bar. The full
    # description is the fallback, since only about 40% carry a parsed
    # requirements section.
    experience_text = "\n".join(part for part in (requirements, description) if part)
    low, high = experience_years(experience_text)
    pay = salary(description)
    return {
        "workplace": workplace(location, title),
        "experience_min_years": low,
        "experience_max_years": high,
        "salary_min": pay.low if pay else None,
        "salary_max": pay.high if pay else None,
        "salary_currency": pay.currency if pay else None,
        "salary_period": pay.period if pay else None,
    }
