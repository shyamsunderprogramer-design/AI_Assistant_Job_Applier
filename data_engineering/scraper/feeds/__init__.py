"""Job feeds: sources searched by title or category, not walked by company.

`run_feeds` runs every feed whose keys are present (keyless ones always), each
isolated from the others' failures, through one filter, one store and one
retirement rule (feeds/common.py). Adzuna predates this package and keeps its
own module; it runs as its own daily stage.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from data_engineering.scraper.feeds import (careerjet, findwork, himalayas, hn_hiring,
                                            jobicy, jooble, remoteok, remotive,
                                            themuse, usajobs, weworkremotely,
                                            workingnomads)
from data_engineering.scraper.feeds.common import PacedSession, PoliteJSON, retire, store

log = logging.getLogger(__name__)

FEEDS = [usajobs, jooble, careerjet, findwork, themuse,
         remotive, remoteok, himalayas, jobicy, hn_hiring, weworkremotely, workingnomads]


@dataclass
class FeedSummary:
    name: str
    label: str
    calls: int = 0
    found: int = 0
    matched: int = 0
    new: int = 0
    seen: int = 0
    duplicates: int = 0
    retired: int = 0
    skipped: str = ""
    error: str = ""

    def line(self) -> str:
        if self.skipped:
            return f"{self.label}: skipped — {self.skipped}"
        if self.error:
            return f"{self.label}: failed — {self.error}"
        return (f"{self.label}: {self.matched} of {self.found} matched · {self.new} new · "
                f"{self.duplicates} already found elsewhere · {self.retired} aged out")


def missing_keys(feed) -> list[str]:
    return [name for name in feed.ENV if not (os.getenv(name) or "").strip()]


def run_feeds(cfg, *, only: list[str] | None = None, dry_run: bool = False,
              polite_client=None, keyed_session=None) -> list[FeedSummary]:
    from data_engineering.scraper.filters import resolve_filter
    from data_engineering.scraper.http_client import HttpSettings, PoliteClient

    job_filter = resolve_filter(cfg)
    titles = list(dict.fromkeys(job_filter.title_keywords))
    client = polite_client or PoliteClient(HttpSettings.from_config(cfg))
    retire_after = int(cfg.get("feeds.close_after_days", 21))
    wanted = [f for f in FEEDS if not only or f.NAME in only]
    disabled = set(cfg.get("feeds.disabled", []) or [])

    summaries = []
    for feed in wanted:
        s = FeedSummary(feed.NAME, feed.LABEL)
        summaries.append(s)
        if feed.NAME in disabled and not only:
            s.skipped = "disabled in config"
            continue
        absent = missing_keys(feed)
        if absent:
            s.skipped = f"needs {', '.join(absent)} in .env"
            continue
        http = (keyed_session() if keyed_session else
                PacedSession(max_calls=int(cfg.get("feeds.max_calls_per_feed", 30)))
                ) if feed.KEYED_API else PoliteJSON(client)
        try:
            jobs = feed.fetch(http, titles, cfg)
        except Exception as exc:              # one feed never stops the others
            log.warning("Feed %s failed: %s", feed.NAME, exc)
            s.error, s.calls = f"{type(exc).__name__}: {str(exc)[:120]}", http.calls
            continue
        s.calls, s.found = http.calls, len(jobs)
        unique = {}
        for job in jobs:
            accept = getattr(feed, "accept", None)
            ok = accept(job, job_filter) if accept else job_filter.matches(job.raw)
            if job.raw.external_id not in unique and ok:
                unique[job.raw.external_id] = job
        s.matched = len(unique)
        if not dry_run:
            s.new, s.seen, s.duplicates = store(list(unique.values()))
            if s.calls:
                s.retired = retire(feed.NAME, retire_after)
    return summaries
