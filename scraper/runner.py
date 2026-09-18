"""Orchestrates a scrape run across every configured/discovered company.

A failing company is logged to scrape_log and the run continues — one broken
board never aborts the batch (README.md §C6).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import uuid
from dataclasses import dataclass, field

from db.models import Company, Job, ScrapeLog, utcnow
from db.session import get_session
from jobfields import derive
from scraper.ashby import AshbyScraper
from scraper.base import CompanyRef, PortalScraper, RawJob
from scraper.discovery import upsert_company
from scraper.workday import WorkdayScraper
from scraper.filters import JobFilter, resolve_filter
from scraper.greenhouse import GreenhouseScraper
from scraper.http_client import HttpSettings, PoliteClient, RobotsDisallowed
from scraper.lever import LeverScraper
from scraper.lifecycle import reconcile_board

log = logging.getLogger(__name__)

SCRAPER_TYPES: dict[str, type[PortalScraper]] = {
    "greenhouse": GreenhouseScraper,
    "lever": LeverScraper,
    "ashby": AshbyScraper,
    "workday": WorkdayScraper,
}


@dataclass
class RunSummary:
    run_id: str
    companies_attempted: int = 0
    companies_failed: int = 0
    jobs_seen: int = 0
    jobs_kept: int = 0
    jobs_new: int = 0
    jobs_updated: int = 0
    jobs_closed: int = 0
    jobs_reopened: int = 0
    failures: list[tuple[str, str, str]] = field(default_factory=list)  # (source, slug, error)


def build_scrapers(cfg, client: PoliteClient) -> dict[str, PortalScraper]:
    """Instantiate the scrapers whose portal is enabled in config."""
    scrapers: dict[str, PortalScraper] = {}
    for name, scraper_cls in SCRAPER_TYPES.items():
        if cfg.get(f"portals.{name}.enabled", True):
            scrapers[name] = scraper_cls(client)
    return scrapers


def sync_seed_companies(cfg) -> int:
    """Push config.yaml's seed_companies into the DB. Idempotent."""
    seeds = cfg.get("companies.seed_companies", []) or []
    for seed in seeds:
        source = str(seed.get("source", "")).lower()
        if source not in SCRAPER_TYPES:
            log.warning("Skipping seed with unknown source: %s", seed)
            continue
        upsert_company(
            name=seed.get("name") or seed["slug"],
            slug=seed["slug"],
            source=source,
            origin="config",
        )
    return len(seeds)


def load_companies(cfg) -> list[CompanyRef]:
    """Every active company in the DB whose portal is enabled."""
    enabled = {name for name in SCRAPER_TYPES if cfg.get(f"portals.{name}.enabled", True)}
    include_discovered = bool(cfg.get("companies.scrape_discovered", True))
    limit = int(cfg.get("limits.max_companies_per_run", 0) or 0)

    with get_session() as session:
        query = session.query(Company).filter(Company.active.is_(True))
        if not include_discovered:
            query = query.filter(Company.origin == "config")
        rows = query.order_by(Company.id).all()

    refs = [
        CompanyRef(name=row.name, slug=row.slug, source=row.source)
        for row in rows
        if row.source in enabled
    ]
    return refs[:limit] if limit > 0 else refs


def run_scrape(cfg) -> RunSummary:
    client = PoliteClient(HttpSettings.from_config(cfg))
    scrapers = build_scrapers(cfg, client)
    job_filter = resolve_filter(cfg)
    summary = RunSummary(run_id=uuid.uuid4().hex[:12])
    deactivate_after = int(cfg.get("limits.deactivate_after_failures", 5) or 0)
    close_on_empty = bool(cfg.get("limits.close_on_empty_board", False))

    companies = load_companies(cfg)
    workers = int(cfg.get("limits.scrape_workers", 6) or 1)
    if _is_in_memory():
        # An in-memory SQLite database belongs to the connection that opened
        # it, so a worker thread finds an empty one. That is a real property
        # of the database, not a test quirk, and the honest response is to
        # stay on one thread rather than to pretend otherwise. Read from the
        # live engine rather than from config, because config is not always
        # where the database came from.
        workers = 1
    log.info("Run %s — scraping %d companies, %d at a time",
             summary.run_id, len(companies), workers)

    # Boards are walked several at a time, but never two on the same host.
    #
    # The 1.5s delay has always been per-host; the loop was not. So four
    # different hosts spent an hour and forty minutes taking turns waiting on
    # each other, of which only forty-six were boards-api.greenhouse.io
    # actually being paced. Workday was the worst of it: seventy boards on
    # seventy DIFFERENT hosts, walked one at a time, thirty-five minutes.
    #
    # Grouping by host and running the groups concurrently changes none of the
    # politeness -- each host still gets 1.5s between requests, enforced by
    # its own lock in PoliteClient -- and turns the total from the SUM of the
    # hosts into the MAX of them.
    from collections import OrderedDict

    by_host: "OrderedDict[str, list]" = OrderedDict()
    for company in companies:
        scraper = scrapers.get(company.source)
        if scraper is None:
            continue
        by_host.setdefault(_host_of(scraper, company), []).append(company)

    lock = threading.Lock()

    def walk(host_companies: list) -> None:
        for company in host_companies:
            _scrape_one(company, scrapers, job_filter, summary, lock,
                        deactivate_after, close_on_empty)

    if workers <= 1:
        # No pool at all, not a pool of one. A ThreadPoolExecutor with a single
        # worker still runs on a worker THREAD, which is exactly what an
        # in-memory database cannot tolerate -- the reason for dropping to one
        # worker in the first place.
        for host_companies in by_host.values():
            walk(host_companies)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(walk, by_host.values()))

    return summary


def _is_in_memory() -> bool:
    """Is the connected database one that cannot be shared between threads?"""
    try:
        from db import session as session_mod

        return ":memory:" in str(getattr(session_mod, "_engine", "") or "")
    except Exception:
        return False


def _host_of(scraper, company) -> str:
    """The host this board lives on — the thing the delay actually applies to.

    Greenhouse, Lever and Ashby put every board on one host each, so those
    stay sequential and must. Every Workday tenant is its own host, so seventy
    of them can be walked at once without any of them noticing.
    """
    try:
        url = scraper.board_url(company.slug) or ""
        if "://" in url:
            return url.split("/")[2].lower()
    except Exception:
        pass
    return company.source


def _scrape_one(company, scrapers, job_filter, summary, lock,
                deactivate_after, close_on_empty) -> None:
    """One board, start to finish. Runs on a worker thread."""
    scraper = scrapers.get(company.source)
    if scraper is None:
        return

    with lock:
        summary.companies_attempted += 1
    started = time.monotonic()
    partial = False
    try:
        raw_jobs, partial = _fetch(scraper, company, job_filter)
    except RobotsDisallowed as exc:
        with lock:
            _log_failure(summary, company, "RobotsDisallowed", str(exc), started,
                         deactivate_after)
        return
    except Exception as exc:
        with lock:
            _log_failure(summary, company, type(exc).__name__, str(exc), started,
                         deactivate_after)
        return

    kept = job_filter.apply(raw_jobs)

    # One writer at a time past this point. SQLite takes an exclusive lock for
    # a write, so letting six threads insert at once buys nothing and produces
    # "database is locked" instead. The slow part — the network — already
    # happened above, outside the lock.
    with lock:
        new_count, updated_count = _persist(kept)

        # Only reachable on a SUCCESSFUL fetch — every failure path above has
        # already returned, so an outage can never close a company's jobs.
        # Reconciled against EVERY posting returned, not the filtered subset:
        # a stored job whose title no longer matches the filters is still on
        # the board (scraper/lifecycle.py).
        lifecycle = reconcile_board(
            company.source,
            company.slug,
            [raw.external_id for raw in raw_jobs],
            close_on_empty_board=close_on_empty,
            partial=partial,
        )

        summary.jobs_seen += len(raw_jobs)
        summary.jobs_kept += len(kept)
        summary.jobs_new += new_count
        summary.jobs_updated += updated_count
        summary.jobs_closed += lifecycle.closed
        summary.jobs_reopened += lifecycle.reopened

        _log_success(summary, company, len(raw_jobs), len(kept), new_count, started)

    log.info(
        "%-11s %-24s %3d seen  %3d match  %3d new  %3d closed",
        company.source, company.slug, len(raw_jobs), len(kept), new_count,
        lifecycle.closed,
    )


def _persist(jobs: list[RawJob]) -> tuple[int, int]:
    """Insert new jobs, refresh changed ones. Dedupe on (source, slug, ext_id)."""
    new_count = 0
    updated_count = 0

    with get_session() as session:
        for raw in jobs:
            content_hash = Job.make_content_hash(raw.title, raw.location, raw.description)
            existing = (
                session.query(Job)
                .filter_by(
                    source=raw.source,
                    company_slug=raw.company_slug,
                    external_id=raw.external_id,
                )
                .one_or_none()
            )

            if existing is None:
                session.add(
                    Job(
                        source=raw.source,
                        company=raw.company,
                        company_slug=raw.company_slug,
                        external_id=raw.external_id,
                        title=raw.title,
                        location=raw.location,
                        description=raw.description,
                        requirements=raw.requirements,
                        application_url=raw.application_url,
                        posted_at=raw.posted_at,
                        content_hash=content_hash,
                        **derive(raw.title, raw.location,
                                 raw.description, raw.requirements),
                    )
                )
                new_count += 1
            elif existing.content_hash != content_hash:
                # Posting was edited — refresh in place, never duplicate.
                existing.title = raw.title
                existing.location = raw.location
                existing.description = raw.description
                existing.requirements = raw.requirements
                existing.application_url = raw.application_url
                existing.content_hash = content_hash
                for field, value in derive(raw.title, raw.location,
                                           raw.description, raw.requirements).items():
                    setattr(existing, field, value)
                existing.exported_to_excel = False  # re-export in Phase 2
                updated_count += 1

    return new_count, updated_count


def _log_success(
    summary: RunSummary, company: CompanyRef, seen: int, kept: int, new: int, started: float
) -> None:
    with get_session() as session:
        session.add(
            ScrapeLog(
                run_id=summary.run_id,
                source=company.source,
                company_slug=company.slug,
                ok=True,
                jobs_seen=seen,
                jobs_kept=kept,
                jobs_new=new,
                duration_seconds=time.monotonic() - started,
            )
        )
        row = session.query(Company).filter_by(source=company.source, slug=company.slug).one_or_none()
        if row is not None:
            row.last_scraped_at = utcnow()
            row.consecutive_failures = 0


def _fetch(scraper, company, job_filter) -> tuple[list[RawJob], bool]:
    """Every posting worth keeping from one board, and whether we saw it all.

    Most boards hand over titles, locations and descriptions together, so there
    is nothing to decide: fetch, then filter.

    Workday does not. Its listing carries no descriptions, each one costs a
    separate request, and a tenant can hold two thousand postings — so the
    scraper splits the work into `stub_jobs` and `fill_details` precisely so a
    filter can run in between. Nothing ran in between. Every Workday board
    fetched descriptions for the first sixty postings in board order and then
    filtered those, which is why the log reads "Citigroup: 999 postings
    matched, fetching the first 60 ... 0 match": the sixty were arbitrary, and
    any posting worth having was somewhere in the other 939.

    Filtering on title and location first turns those sixty requests into sixty
    *relevant* requests.

    The second return value is whether the board was read completely. It was
    not, whenever filtering left more candidates than `fill_details` will
    fetch — and a board we could not read completely must not close anything.
    """
    stubs = getattr(scraper, "stub_jobs", None)
    if stubs is None:
        return scraper.fetch_jobs(company), False

    listed = stubs(company)
    candidates = [job for job in listed if job_filter.matches_stub(job)]
    filled = scraper.fill_details(company, candidates)

    if len(listed) > len(candidates):
        log.info("%s: %d of %d postings worth a description", company.name,
                 len(candidates), len(listed))
    return filled, len(filled) < len(listed)


def _log_failure(
    summary: RunSummary,
    company: CompanyRef,
    error_type: str,
    message: str,
    started: float,
    deactivate_after: int = 0,
) -> None:
    summary.companies_failed += 1
    summary.failures.append((company.source, company.slug, f"{error_type}: {message}"))
    log.warning("FAILED %s/%s — %s: %s", company.source, company.slug, error_type, message)

    with get_session() as session:
        session.add(
            ScrapeLog(
                run_id=summary.run_id,
                source=company.source,
                company_slug=company.slug,
                ok=False,
                error_type=error_type,
                error_message=message[:2000],
                duration_seconds=time.monotonic() - started,
            )
        )
        row = session.query(Company).filter_by(source=company.source, slug=company.slug).one_or_none()
        if row is not None:
            row.consecutive_failures += 1
            # A dead slug shouldn't be re-probed forever once the list is large.
            if deactivate_after and row.consecutive_failures >= deactivate_after:
                row.active = False
                log.warning(
                    "Deactivating %s/%s after %d consecutive failures",
                    company.source, company.slug, row.consecutive_failures,
                )
