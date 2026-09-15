"""What to apply to, and in what order.

A posting's age is the strongest thing we know about its odds. A recruiter
reads the first applications to arrive and skims the rest; a role posted an
hour ago has an empty pile, and the same role in three weeks has four hundred
CVs in front of yours. So the queue is ordered by freshness band first and
match score only within a band — a 95% match posted a month ago is worth less
than an 80% match posted this morning, and ordering by score alone gets that
exactly backwards.

Bands, freshest first:

    0   posted within a day          apply now, today, before anyone else
    1   posted one to three days ago
    2   posted three to seven days ago
    3   older, with a known date
    4   no date, but first seen today   (treat as new-ish: we only just met it)
    5   no date, seen before

Band 4 exists because some boards never say when a posting went up. `found_at`
is then the only signal, and "this appeared in today's scrape" is real
evidence, just weaker than a stated date — so it sits below every dated
posting that is genuinely recent, and above the undated backlog.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Only sources with a form filler. Adding one here without writing its filler
# would queue applications that cannot be submitted.
FILLABLE = ("greenhouse",)

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

    @property
    def band_name(self) -> str:
        return BAND_NAMES.get(self.band, "unknown")

    @property
    def score_pct(self) -> int | None:
        return None if self.score is None else round(self.score * 100)


def _company_key(name: str) -> str:
    return " ".join((name or "").split()).lower()


# How long to leave between two applications to the same employer. Half a day,
# so a company with several good roles hears from you this afternoon and again
# tomorrow morning, rather than five times in one minute.
DEFAULT_COOLDOWN_HOURS = 12


def build_queue(jobs, *, limit: int, min_score: float | None = None,
                sources=FILLABLE, max_per_company: int = 1,
                last_applied: dict | None = None,
                cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
                now: datetime | None = None) -> list[Candidate]:
    """Order postings for applying, freshest band first, best match within it.

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
        if not (job.application_url or "").strip():
            continue                       # nowhere to apply
        score = job.ats_match_score
        if min_score is not None and (score is None or score < min_score):
            continue

        candidates.append(Candidate(
            job_id=job.id, company=job.company, title=job.title,
            url=job.application_url, source=job.source, score=score,
            band=freshness_band(job.posted_at, job.found_at, now),
        ))

    # Band first, then the best match inside the band. An unscored posting
    # sorts last within its band rather than first.
    candidates.sort(key=lambda c: (c.band, -(c.score if c.score is not None else -1)))

    # Now thin to one per company. Because the list is already in the order we
    # want, the first time a company appears IS its best posting — freshest
    # band, best score within it — so keeping the first is keeping the right
    # one, with no second pass to decide.
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
    """One line per band, so a run says what it is about to do before it does it."""
    if not queue:
        return "nothing to apply to"
    counts: dict[int, int] = {}
    for candidate in queue:
        counts[candidate.band] = counts.get(candidate.band, 0) + 1
    return " · ".join(
        f"{counts[band]} {BAND_NAMES[band]}" for band in sorted(counts)
    )
