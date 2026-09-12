"""How old a posting is, in words.

The sheet already carries an absolute posting date, which answers "when" but
not "how long ago" — and the age is what decides whether a posting is worth
opening. Open roles here run from hours old to five and a half years, so the
scale has to stay readable across that whole span rather than pick one unit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scraper.lifecycle import as_utc

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
WEEK = 7 * DAY
MONTH = 30.44 * DAY     # mean calendar month, so "12 mo" and "1 yr" agree
YEAR = 365.25 * DAY


def humanize(delta: timedelta | None) -> str:
    """A short, readable age: "just now", "5 hr", "3 days", "5.5 yr"."""
    if delta is None:
        return ""
    seconds = delta.total_seconds()
    # A posting dated in the future is a board's bad data, not a negative age.
    if seconds < MINUTE:
        return "just now"
    if seconds < HOUR:
        return f"{int(seconds // MINUTE)} min"
    if seconds < DAY:
        return f"{int(seconds // HOUR)} hr"
    if seconds < WEEK:
        days = int(seconds // DAY)
        return "1 day" if days == 1 else f"{days} days"
    if seconds < 5 * WEEK:
        weeks = int(seconds // WEEK)
        return "1 wk" if weeks == 1 else f"{weeks} wks"
    if seconds < YEAR:
        return f"{int(seconds // MONTH)} mo"
    years = seconds / YEAR
    # A decimal is the difference between "old" and "abandoned years ago".
    return f"{years:.0f} yr" if years >= 10 else f"{years:.1f} yr"


def posting_date(job) -> tuple[datetime | None, bool]:
    """(when the posting went up, whether the board actually said so).

    `posted_at` is nullable — some boards publish no date — so fall back to
    when we first saw it, the convention the stats command already uses. But
    first-seen is a floor, not the truth: a posting we met yesterday may have
    been up for a year. Callers mark that estimate rather than state it.
    """
    posted = as_utc(getattr(job, "posted_at", None))
    if posted is not None:
        return posted, True
    return as_utc(getattr(job, "found_at", None)), False


def age_of(job, now: datetime | None = None) -> timedelta | None:
    """How long since a posting went up, or None when no date is known."""
    when, _ = posting_date(job)
    if when is None:
        return None
    return (now or datetime.now(timezone.utc)) - when


def age_days(job, now: datetime | None = None) -> float | None:
    """Age in days, for filtering. None when the posting carries no date."""
    delta = age_of(job, now)
    return None if delta is None else delta.total_seconds() / DAY


def age_label(job, now: datetime | None = None) -> str:
    """The displayable age, or "" when the posting carries no date.

    An age we inferred from first-seen is prefixed "~", because the board never
    told us when it went up and the real posting may be far older.
    """
    when, exact = posting_date(job)
    if when is None:
        return ""
    label = humanize((now or datetime.now(timezone.utc)) - when)
    return label if exact else f"~{label}"
