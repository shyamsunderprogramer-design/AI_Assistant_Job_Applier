"""Job postings from Adzuna's official search API.

Adzuna aggregates postings from thousands of job boards and company career
sites into one searchable index, and offers it through a free, keyed API
(developer.adzuna.com). That reaches employers no board scraper here can:
anyone not on Greenhouse, Lever, Ashby, Workable or Workday.

HOW IT DIFFERS FROM THE BOARD SCRAPERS
  * It searches by job TITLE, not by company: one query per title from the
    person's derived search profile, US only, newest first.
  * It is an API used under the person's own key and Adzuna's API terms, not
    a crawl. api.adzuna.com's robots.txt disallows everything — that file is
    addressed to crawlers — so these calls do not go through PoliteClient,
    whose robots rule has no exceptions (README §C5). They are paced here
    instead, and capped per run well inside the free tier's daily limit.
  * The description is Adzuna's snippet (a few hundred characters), not the
    full posting. The scorer already treats a short description as weaker
    evidence (`scorer.score_basis`), so these rank honestly below postings
    whose full text was read.
  * A posting Adzuna also found on a board this tool scrapes directly is
    skipped: the board's copy has the full description and a fillable form.

Nothing runs without ADZUNA_APP_ID and ADZUNA_APP_KEY in .env.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from data_engineering.scraper.base import RawJob

log = logging.getLogger(__name__)

SOURCE = "adzuna"
API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
# The free tier allows about 25 calls a minute and 250 a day. One call every
# three seconds and a per-run cap keep a daily run far inside both.
MIN_INTERVAL_S = 3.0
DEFAULT_MAX_CALLS = 40
DEFAULT_PAGES = 2
PER_PAGE = 50


def credentials() -> tuple[str, str] | None:
    app_id = (os.getenv("ADZUNA_APP_ID") or "").strip()
    app_key = (os.getenv("ADZUNA_APP_KEY") or "").strip()
    return (app_id, app_key) if app_id and app_key else None


def _slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (company or "").lower()).strip("-") or "unknown"


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class AdzunaJob:
    raw: RawJob
    salary_min: int | None = None
    salary_max: int | None = None


def parse_result(result: dict) -> AdzunaJob | None:
    """One API result, or None when it lacks what a posting needs."""
    title = _clean(result.get("title"))
    company = _clean((result.get("company") or {}).get("display_name"))
    url = result.get("redirect_url") or ""
    job_id = str(result.get("id") or "")
    if not (title and company and url and job_id):
        return None

    # Adzuna estimates a salary when the posting states none. An estimate is
    # not the employer's number, so only a stated one is kept.
    predicted = str(result.get("salary_is_predicted", "1")) != "0"
    low, high = result.get("salary_min"), result.get("salary_max")
    return AdzunaJob(
        raw=RawJob(
            source=SOURCE,
            company=company,
            company_slug=_slug(company),
            external_id=job_id,
            title=title,
            location=_clean((result.get("location") or {}).get("display_name")) or None,
            description=_clean(result.get("description")) or None,
            application_url=url,
            posted_at=_when(result.get("created")),
        ),
        salary_min=None if predicted or low is None else int(low),
        salary_max=None if predicted or high is None else int(high),
    )


class AdzunaClient:
    """Keyed calls to the search API, paced and counted."""

    def __init__(self, app_id: str, app_key: str, *, session=None, max_calls: int = DEFAULT_MAX_CALLS,
                 sleep=time.sleep, clock=time.monotonic):
        import requests

        self.app_id, self.app_key = app_id, app_key
        self.session = session or requests.Session()
        self.max_calls, self.calls = max_calls, 0
        self._sleep, self._clock, self._last = sleep, clock, None

    def search(self, title: str, *, page: int = 1, country: str = "us",
               max_days_old: int = 3) -> list[dict]:
        if self.calls >= self.max_calls:
            return []
        if self._last is not None:
            wait = MIN_INTERVAL_S - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        self.calls += 1
        resp = self.session.get(
            API.format(country=country, page=page),
            params={"app_id": self.app_id, "app_key": self.app_key,
                    "title_only": title, "max_days_old": max_days_old,
                    "results_per_page": PER_PAGE, "sort_by": "date",
                    "content-type": "application/json"},
            timeout=30,
        )
        if resp.status_code in (401, 403):
            raise PermissionError("Adzuna refused the key — check ADZUNA_APP_ID and ADZUNA_APP_KEY")
        if resp.status_code == 429:
            log.warning("Adzuna rate limit reached — stopping this run's searches")
            self.calls = self.max_calls
            return []
        resp.raise_for_status()
        return (resp.json() or {}).get("results") or []


def _key(company: str, title: str) -> tuple[str, str]:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return norm(company), norm(title)


def collect(client: AdzunaClient, titles: list[str], job_filter, *,
            pages: int = DEFAULT_PAGES, max_days_old: int = 3) -> list[AdzunaJob]:
    """Every posting the searches return that passes the person's filter."""
    found: dict[str, AdzunaJob] = {}
    for title in titles:
        for page in range(1, pages + 1):
            results = client.search(title, page=page, max_days_old=max_days_old)
            for result in results:
                job = parse_result(result)
                if job and job.raw.external_id not in found and job_filter.matches(job.raw):
                    found[job.raw.external_id] = job
            if len(results) < PER_PAGE:
                break                      # no further pages for this title
    return list(found.values())


def store(jobs: list[AdzunaJob]) -> tuple[int, int, int]:
    """Write postings. Returns (new, already had, skipped as duplicates of a board)."""
    from data_engineering.db.models import Job, utcnow
    from data_engineering.db.session import get_session

    new = seen = duplicates = 0
    with get_session() as session:
        elsewhere = {_key(c, t) for c, t in session.query(Job.company, Job.title)
                     .filter(Job.is_open.is_(True), Job.source != SOURCE).all()}
        for job in jobs:
            raw = job.raw
            existing = (session.query(Job)
                        .filter_by(source=SOURCE, external_id=raw.external_id).one_or_none())
            if existing is not None:
                existing.last_seen_at = utcnow()
                seen += 1
                continue
            if _key(raw.company, raw.title) in elsewhere:
                duplicates += 1
                continue
            session.add(Job(
                source=SOURCE, company=raw.company, company_slug=raw.company_slug,
                external_id=raw.external_id, title=raw.title, location=raw.location,
                description=raw.description, requirements=raw.description,
                application_url=raw.application_url, posted_at=raw.posted_at,
                salary_min=job.salary_min, salary_max=job.salary_max,
                salary_currency="USD" if job.salary_min or job.salary_max else None,
                salary_period="year" if job.salary_min or job.salary_max else None,
                content_hash=hashlib.sha256(
                    f"{SOURCE}{raw.external_id}{raw.title}".encode()).hexdigest(),
                is_open=True,
            ))
            new += 1
    return new, seen, duplicates


@dataclass
class AdzunaSummary:
    searched: int = 0
    calls: int = 0
    matched: int = 0
    new: int = 0
    seen: int = 0
    duplicates: int = 0
    retired: int = 0
    skipped: str = ""


def run(cfg, *, dry_run: bool = False, client: AdzunaClient | None = None) -> AdzunaSummary:
    """Search Adzuna for the person's titles and store what matches."""
    from data_engineering.scraper.filters import resolve_filter

    summary = AdzunaSummary()
    keys = credentials()
    if client is None and keys is None:
        summary.skipped = "no ADZUNA_APP_ID / ADZUNA_APP_KEY in .env"
        return summary

    job_filter = resolve_filter(cfg)
    titles = list(dict.fromkeys(job_filter.title_keywords))[: int(cfg.get("adzuna.max_titles", 14))]
    client = client or AdzunaClient(*keys, max_calls=int(cfg.get("adzuna.max_calls", DEFAULT_MAX_CALLS)))
    jobs = collect(client, titles, job_filter,
                   pages=int(cfg.get("adzuna.pages_per_title", DEFAULT_PAGES)),
                   max_days_old=int(cfg.get("adzuna.max_days_old", 3)))
    summary.searched, summary.calls, summary.matched = len(titles), client.calls, len(jobs)
    if not dry_run:
        summary.new, summary.seen, summary.duplicates = store(jobs)
        if client.calls:           # an outage searched nothing, and proves nothing
            summary.retired = retire(int(cfg.get("adzuna.close_after_days", 21)))
    return summary


def retire(after_days: int) -> int:
    """Close Adzuna postings not seen in `after_days`.

    Only fresh postings are asked for, so a posting stops being seen as it
    ages, not only when it is filled. There is no board to check it against;
    age is the honest signal, and a three-week-old posting has had its pile.
    """
    from datetime import timedelta

    from data_engineering.db.models import Job, utcnow
    from data_engineering.db.session import get_session

    if after_days <= 0:
        return 0
    cutoff = (utcnow() - timedelta(days=after_days)).replace(tzinfo=None)
    with get_session() as session:
        stale = (session.query(Job).filter(Job.source == SOURCE, Job.is_open.is_(True),
                                           Job.last_seen_at < cutoff).all())
        for job in stale:
            job.is_open = False
            job.closed_at = utcnow()
        return len(stale)
