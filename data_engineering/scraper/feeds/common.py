"""What every job feed shares: the posting shape, HTTP, storage and retirement.

A feed is a source searched by job title or category rather than walked
company by company: public job APIs (Remotive, The Muse, ...) and keyed
official APIs (USAJOBS, Jooble, ...). Each lives in its own module in this
package, exposing:

    NAME       the `source` stored on each posting
    LABEL      what the person sees
    ENV        env vars it needs (empty when it needs none)
    KEYED_API  True when its host's robots.txt disallows crawlers but the API
               is offered for keyed use — see README §C5
    fetch(http, titles, cfg) -> list[FeedJob]

Everything after fetch is here, so every feed is filtered, de-duplicated,
stored and retired the same way.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from data_engineering.scraper.base import RawJob

log = logging.getLogger(__name__)


@dataclass
class FeedJob:
    raw: RawJob
    salary_min: int | None = None
    salary_max: int | None = None
    currency: str | None = None
    period: str | None = None       # "year" | "hour"


# -- small parsers ----------------------------------------------------------

def clean(text) -> str:
    """Plain text from an HTML fragment, whitespace collapsed."""
    text = re.sub(r"<(br|/p|/li|/div)[^>]*>", "\n", str(text or ""), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (company or "").lower()).strip("-") or "unknown"


def when(value) -> datetime | None:
    """ISO 8601, Unix seconds, or RFC 2822 — every feed picks one."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.isdigit():
        stamp = int(text)
        return datetime.fromtimestamp(stamp / 1000 if stamp > 10**11 else stamp, timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def money(value) -> int | None:
    try:
        number = int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def posting(source: str, *, job_id, title, company, url, location=None,
            description=None, posted=None) -> RawJob | None:
    """A RawJob, or None when the feed left out something a posting needs."""
    title, company = clean(title), clean(company)
    if not (title and company and url and str(job_id or "")):
        return None
    return RawJob(source=source, company=company, company_slug=slug(company),
                  external_id=str(job_id)[:128], title=title,
                  location=(clean(location) or None), description=(clean(description) or None),
                  application_url=url, posted_at=when(posted))


# -- HTTP -------------------------------------------------------------------

class PoliteJSON:
    """PoliteClient with robots.txt, pacing and backoff — for every feed whose
    host allows it."""

    def __init__(self, client):
        self.client, self.calls = client, 0

    def get_json(self, url, params=None, headers=None):
        self.calls += 1
        return self.client.get_json(url, params=params, headers=headers or {})

    def get_text(self, url, params=None, headers=None):
        """For feeds published as RSS/XML rather than JSON."""
        self.calls += 1
        resp = self.client.get(url, params=params, headers=headers or {})
        resp.raise_for_status()
        return resp.text

    def post_json(self, url, payload, headers=None):
        self.calls += 1
        resp = self.client.post(url, json=payload, headers=headers or {})
        resp.raise_for_status()
        return resp.json()


class PacedSession:
    """For keyed official APIs whose robots.txt addresses crawlers only.

    One call every `interval` seconds and at most `max_calls` per run: the
    politeness PoliteClient would give, without its robots rule.
    """

    def __init__(self, *, interval: float = 3.0, max_calls: int = 30, session=None,
                 user_agent: str = "job-applier (personal job search)",
                 sleep=time.sleep, clock=time.monotonic):
        import requests

        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", user_agent)
        self.interval, self.max_calls, self.calls = interval, max_calls, 0
        self._sleep, self._clock, self._last = sleep, clock, None

    def _turn(self) -> bool:
        if self.calls >= self.max_calls:
            return False
        if self._last is not None:
            wait = self.interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        self.calls += 1
        return True

    def _json(self, resp):
        if resp.status_code in (401, 403):
            raise PermissionError(f"key refused (HTTP {resp.status_code})")
        if resp.status_code == 429:
            self.calls = self.max_calls          # the API asked us to stop
            return None
        resp.raise_for_status()
        return resp.json()

    def get_json(self, url, params=None, headers=None):
        if not self._turn():
            return None
        return self._json(self.session.get(url, params=params, headers=headers or {}, timeout=30))

    def post_json(self, url, payload, headers=None):
        if not self._turn():
            return None
        return self._json(self.session.post(url, json=payload, headers=headers or {}, timeout=30))


# -- storage ----------------------------------------------------------------

def _key(company: str, title: str) -> tuple[str, str]:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return norm(company), norm(title)


def store(jobs: list[FeedJob]) -> tuple[int, int, int]:
    """Write postings. Returns (new, already had, skipped as found elsewhere).

    A posting already open from any OTHER source is skipped: a board's copy
    has the full description and often a fillable form, and one feed's copy
    of another feed's posting adds nothing but a duplicate row.
    """
    from data_engineering.db.models import Job, utcnow
    from data_engineering.db.session import get_session

    new = seen = duplicates = 0
    with get_session() as session:
        open_rows = session.query(Job.source, Job.company, Job.title).filter(Job.is_open.is_(True)).all()
        for job in jobs:
            raw = job.raw
            existing = (session.query(Job)
                        .filter_by(source=raw.source, external_id=raw.external_id).one_or_none())
            if existing is not None:
                existing.last_seen_at = utcnow()
                seen += 1
                continue
            key = _key(raw.company, raw.title)
            if any(src != raw.source and _key(c, t) == key for src, c, t in open_rows):
                duplicates += 1
                continue
            has_pay = bool(job.salary_min or job.salary_max)
            session.add(Job(
                source=raw.source, company=raw.company, company_slug=raw.company_slug,
                external_id=raw.external_id, title=raw.title, location=raw.location,
                description=raw.description, requirements=raw.description,
                application_url=raw.application_url, posted_at=raw.posted_at,
                salary_min=job.salary_min, salary_max=job.salary_max,
                salary_currency=(job.currency or "USD") if has_pay else None,
                salary_period=(job.period or "year") if has_pay else None,
                content_hash=hashlib.sha256(
                    f"{raw.source}{raw.external_id}{raw.title}".encode()).hexdigest(),
                is_open=True,
            ))
            open_rows.append((raw.source, raw.company, raw.title))
            new += 1
    return new, seen, duplicates


def retire(source: str, after_days: int) -> int:
    """Close a feed's postings not seen for `after_days`.

    Feeds are asked for recent postings, so a posting stops appearing as it
    ages as well as when it is filled. Age is the only honest signal left.
    """
    from data_engineering.db.models import Job, utcnow
    from data_engineering.db.session import get_session

    if after_days <= 0:
        return 0
    cutoff = (utcnow() - timedelta(days=after_days)).replace(tzinfo=None)
    with get_session() as session:
        stale = (session.query(Job).filter(Job.source == source, Job.is_open.is_(True),
                                           Job.last_seen_at < cutoff).all())
        for job in stale:
            job.is_open, job.closed_at = False, utcnow()
        return len(stale)
