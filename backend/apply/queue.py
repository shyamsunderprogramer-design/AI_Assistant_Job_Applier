"""What to apply to, and in what order.

The person's order (23 Oct 2026, "watchlist like and most fitted to jobs"): the jobs they
saved to the Watch list first, then roles at the companies they watch, then the best fit of
everything else. In each, the best fit leads; at the same whole-percent fit the fresher posting
goes first, because a recruiter reads the first applications to arrive and skims the rest.
Until that day freshness led -- a 95% match from a month ago went after an 80% one from the
morning -- and fit only broke ties.

Freshness bands, for that tie and for saying what a run is about to do:

    0   posted within a day
    1   posted one to three days ago
    2   posted three to seven days ago
    3   older, with a known date
    4   no date, but first seen today   (treat as new-ish: we only just met it)
    5   no date, seen before

Band 4 exists because some boards never say when a posting went up. `found_at`
is then the only signal, and "this appeared in today's scrape" is real
evidence, just weaker than a stated date — so it sits below every dated
posting that is genuinely recent, and above the undated backlog.

Past `apply.max_age_months` (4) a posting is not picked at all: the person
applies to nothing older (23 Oct 2026).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backend.core.jobage import DAY, MONTH, age_days, posting_date
from backend.core.joblinks import apply_link

# Only sources with a form filler. Adding one here without writing its filler
# would queue applications that cannot be submitted.
FILLABLE = ("greenhouse", "workday", "jazzhr")

# A status the user has touched is theirs. Never re-apply, never overwrite.
APPLIABLE_STATUSES = ("Not Applied", "Manual Review")

BAND_NAMES = {
    0: "under a day",
    1: "1-3 days",
    2: "3-7 days",
    3: "older",
    4: "no date, seen today",
    5: "no date, seen earlier",
}

# Who put a job in the queue, in the order they go. CHOSEN is a run of the jobs the person picked on
# the Jobs page (24 Oct 2026); such a run holds nothing else, so where it sorts does not matter.
SAVED, WATCHED, BEST_FIT, CHOSEN = 0, 1, 2, 3
GROUP_NAMES = {SAVED: "saved by you", WATCHED: "at a company you watch", BEST_FIT: "best fit",
               CHOSEN: "chosen by you"}


def _as_utc(when) -> datetime | None:
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def freshness_band(posted_at, found_at, now: datetime | None = None) -> int:
    """Which band a posting falls in. Lower is fresher, and applied to sooner."""
    now = now or datetime.now(timezone.utc)
    posted, found = _as_utc(posted_at), _as_utc(found_at)

    if posted is not None:
        age = now - posted
        if age <= timedelta(days=1):
            return 0
        if age <= timedelta(days=3):
            return 1
        if age <= timedelta(days=7):
            return 2
        return 3

    # No stated date. Having first seen it today is weaker evidence than a
    # date, but it is evidence.
    if found is not None and (now - found) <= timedelta(days=1):
        return 4
    return 5


@dataclass(frozen=True)
class Candidate:
    """One posting worth applying to, with why it is placed where it is."""

    job_id: int
    company: str
    title: str
    url: str
    source: str
    score: float | None
    band: int
    group: int = BEST_FIT

    @property
    def band_name(self) -> str:
        return BAND_NAMES.get(self.band, "unknown")

    @property
    def group_name(self) -> str:
        return GROUP_NAMES.get(self.group, "unknown")

    @property
    def score_pct(self) -> int | None:
        return None if self.score is None else round(self.score * 100)


def _company_key(name: str) -> str:
    return " ".join((name or "").split()).lower()


# How long to leave between two applications to the same employer. Half a day,
# so a company with several good roles hears from you this afternoon and again
# tomorrow morning, rather than five times in one minute.
DEFAULT_COOLDOWN_HOURS = 12

# The oldest posting the person applies to, in months (23 Oct 2026: "I don't want more than
# 4 months job postings"). 0 in config.yaml lifts the limit.
DEFAULT_MAX_AGE_MONTHS = 4


def age_limit_months(cfg) -> float:
    return float(cfg.get("apply.max_age_months", DEFAULT_MAX_AGE_MONTHS) or 0)


def age_limit_days(cfg) -> float | None:
    """`apply.max_age_months` in days, or None when there is no limit."""
    months = age_limit_months(cfg)
    return months * MONTH / DAY if months > 0 else None


def too_old(job, limit_days: float | None, now: datetime | None = None) -> bool:
    """Older than the limit. A posting with no date counts from when it was first seen, a
    floor on its age: one first seen five months ago has been up five months at least. One
    with no date at all is kept: not knowing its age is not evidence that it is old."""
    if limit_days is None:
        return False
    age = age_days(job, now)
    return age is not None and age > limit_days


def age_note(job, months: float, now: datetime | None = None) -> str:
    """Why a posting is past the limit. In days: the Jobs page's "4 mo" is a posting 122 to
    152 days old, and "4 mo, over the 4-month limit" would read as a contradiction."""
    _, stated = posting_date(job)
    return (f"{'posted' if stated else 'first seen'} {age_days(job, now):.0f} days ago, "
            f"over the {months:g}-month limit")


def _order(c: Candidate) -> tuple:
    """Saved, then watched, then the rest; best fit first in each, by the whole percent the
    person sees; at the same percent, the fresher posting. Unscored sorts last in its group."""
    pct = c.score_pct if c.score_pct is not None else -1
    return (c.group, -pct, c.band, -(c.score if c.score is not None else -1))


def build_queue(jobs, *, limit: int, min_score: float | None = None,
                sources=FILLABLE, max_per_company: int = 1,
                last_applied: dict | None = None,
                cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
                max_age_days: float | None = None,
                saved: Collection[int] = (), watched: Collection[int] = (),
                chosen_ids: Collection[int] = (),
                now: datetime | None = None) -> list[Candidate]:
    """Order postings for applying: saved, watched, then best fit (see the module's note).

    `max_per_company` is the one limit that matters for how this looks from the
    other side. An employer cannot see that you applied to anyone else — their
    dashboard holds their own board and nothing more — but they see every
    application to *them*, with timestamps. Several arriving together is the
    one pattern that is reliably visible, and it reads as spraying rather than
    interest.

    It is not a hypothetical: of the open matches right now, SpaceX has 74,
    Accenture Federal 66, GRVTY 31, and 121 companies have more than one. Left
    alone, the queue would send seventy-four applications to one employer in an
    afternoon. One per company, the best one, is both better manners and a
    better application.

    So applications to one employer are SPACED rather than capped: their best
    role goes now, their next-best after `cooldown_hours`, and so on. Over a
    week a company with several good roles receives several applications, one
    at a time, which is what a person doing this by hand would produce anyway.
    `last_applied` maps a company to when it last heard from you; a company
    absent from it, or with None, has never been applied to and is free.

    `max_age_days` leaves out postings older than that (see `too_old`); None
    keeps every age.

    `saved` are the ids of the jobs the person saved to the Watch list: first, and at any fit,
    since they chose them. `watched` are the ids of jobs at companies they watch: next, at the
    usual fit. Every other rule holds for both. `chosen_ids` names the jobs the person picked on
    the Jobs page, for a run of those alone (runner.chosen_queue).

    `jobs` is any iterable of rows with the attributes used below, so this can
    be tested without a database.
    """
    candidates: list[Candidate] = []
    for job in jobs:
        if not getattr(job, "is_open", True):
            continue
        if job.status not in APPLIABLE_STATUSES:
            continue                       # already applied, or the user's call
        if job.source not in sources:
            continue                       # no filler for this board yet
        link = apply_link(job)             # this job's own page on the company's site first
        if not (link or "").strip():
            continue                       # nowhere to apply
        if too_old(job, max_age_days, now):
            continue                       # older than the person applies to
        score = job.ats_match_score
        if (min_score is not None and job.id not in saved
                and (score is None or score < min_score)):
            continue

        candidates.append(Candidate(
            job_id=job.id, company=job.company, title=job.title,
            url=link, source=job.source, score=score,
            band=freshness_band(job.posted_at, job.found_at, now),
            group=(CHOSEN if job.id in chosen_ids else SAVED if job.id in saved
                   else WATCHED if job.id in watched else BEST_FIT),
        ))

    candidates.sort(key=_order)

    # Now thin to one per company. Because the list is already in the order we
    # want, the first time a company appears IS its best posting — its saved
    # job if there is one, else its best fit — so keeping the first is keeping
    # the right one, with no second pass to decide.
    now = now or datetime.now(timezone.utc)
    cooldown = timedelta(hours=max(0, cooldown_hours))
    recent = {_company_key(name): _as_utc(when)
              for name, when in (last_applied or {}).items()}

    seen: dict[str, int] = {}
    chosen: list[Candidate] = []
    for candidate in candidates:
        key = _company_key(candidate.company)

        # Still cooling down from the last application to this employer. Not
        # "never again" — just not yet.
        last = recent.get(key)
        if last is not None and (now - last) < cooldown:
            continue
        if seen.get(key, 0) >= max_per_company:
            continue

        seen[key] = seen.get(key, 0) + 1
        chosen.append(candidate)
        if len(chosen) >= limit:
            break
    return chosen


def summarise(queue: list[Candidate]) -> str:
    """Who chose them and how fresh they are, so a run says what it is about to do first."""
    if not queue:
        return "nothing to apply to"
    groups = Counter(c.group for c in queue)
    bands = Counter(c.band for c in queue)
    return (" · ".join(f"{groups[g]} {GROUP_NAMES[g]}" for g in sorted(groups)) + "; "
            + " · ".join(f"{bands[b]} {BAND_NAMES[b]}" for b in sorted(bands)))
