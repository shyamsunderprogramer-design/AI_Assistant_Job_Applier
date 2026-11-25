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

from data_engineering.db.models import Company, Job, ScrapeLog, utcnow
from data_engineering.db.session import get_session
from backend.core.jobfields import derive
from data_engineering.scraper.ashby import AshbyScraper
from data_engineering.scraper.base import CompanyRef, PortalScraper, RawJob
from data_engineering.scraper.discovery import upsert_company
from data_engineering.scraper.workable import WorkableScraper
from data_engineering.scraper.recruitee import RecruiteeScraper
from data_engineering.scraper.teamtailor import TeamtailorScraper
from data_engineering.scraper.bamboohr import BambooHRScraper
from data_engineering.scraper.breezy import BreezyScraper
from data_engineering.scraper.jazzhr import JazzHRScraper
from data_engineering.scraper.jobvite import JobviteScraper
from data_engineering.scraper.workday import WorkdayScraper
from data_engineering.scraper.filters import JobFilter, resolve_filter
from data_engineering.scraper.greenhouse import GreenhouseScraper
from data_engineering.scraper.http_client import (HttpSettings, NetworkError, PoliteClient, RateLimited,
                                                  RobotsDisallowed, network_ok)
from data_engineering.scraper.lever import LeverScraper
from data_engineering.scraper.lifecycle import reconcile_board

log = logging.getLogger(__name__)

SCRAPER_TYPES: dict[str, type[PortalScraper]] = {
    "greenhouse": GreenhouseScraper,
    "lever": LeverScraper,
    "ashby": AshbyScraper,
    "workday": WorkdayScraper,
    "workable": WorkableScraper,
    "recruitee": RecruiteeScraper,
    "teamtailor": TeamtailorScraper,
    "bamboohr": BambooHRScraper,
    "breezy": BreezyScraper,
    "jazzhr": JazzHRScraper,
    "jobvite": JobviteScraper,
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
    # Boards a host turned away with 429s, tried again once it has cooled off.
    deferred: list = field(default_factory=list)
    # Each board's outcome, for a partition's coverage report (devops/discovery_worker.py):
    # (source, slug) -> (status, error_type). status: complete | partial | failed | rate_limited | not_attempted.
    # "partial": read, but its listing or its relevant descriptions are incomplete; "failed": a board-level error
    # (404, robots, parse, or no answer). not_attempted with "NetworkDown": this machine lost the internet first.
    outcomes: dict = field(default_factory=dict)
    # Each board read, at the posting level (Nov 2026): (source, slug) -> {listing: complete|incomplete, listed,
    # relevant, described, pending, skipped}. relevant: postings the search wants a description of; skipped: the
    # rest, intentionally not described; pending: relevant postings whose description is still missing.
    details: dict = field(default_factory=dict)
    network_down: bool = False          # the run stopped because nothing answered at all (NetworkWatch)


COMPLETE, PARTIAL, FAILED, RATE_LIMITED, NOT_ATTEMPTED = "complete", "partial", "failed", "rate_limited", "not_attempted"
NETWORK_DOWN = "NetworkDown"


class NetworkWatch:
    """Tells "this board's host did not answer" apart from "nothing answers": an outage morning once read for
    hours with no internet, recorded thousands of failures and retired healthy boards.

    Every board that fails with no answer at all (NetworkError) adds to a streak shared by all workers; any board
    that answers resets it. At `streak` in a row the internet itself is checked (http_client.network_ok, one check
    at a time). Unreachable: the run is DOWN -- no further board is started, and the boards that failed in the
    streak are recorded as not attempted, never as failed. Reachable: those were the boards' own hosts."""

    def __init__(self, probe=network_ok, streak: int = 5):
        self.probe, self.limit = probe, max(1, int(streak))
        self.down = False
        self._streak = 0
        self._lock = threading.Lock()
        self._probing = threading.Lock()

    def answered(self) -> None:
        with self._lock:
            self._streak = 0

    def silent(self) -> bool:
        """A board got no answer. Returns whether the network is down."""
        with self._lock:
            self._streak += 1
            due = self._streak >= self.limit and not self.down
        if due and self._probing.acquire(blocking=False):
            try:
                if not self.probe():
                    self.down = True
                    log.warning("nothing answers: this machine has no internet -- the run stops; the boards not "
                                "read stay as they were, none of them counted as failed")
                else:
                    with self._lock:
                        self._streak = 0
            finally:
                self._probing.release()
        return self.down


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


def load_companies(cfg, sites=None, skip_sites=None) -> list[CompanyRef]:
    """Every active company in the DB whose portal is enabled -- of `sites` only, when given, and
    none of `skip_sites`: one part of the GitHub fetch, split four ways (devops/fetch_parts.py)."""
    enabled = {name for name in SCRAPER_TYPES if cfg.get(f"portals.{name}.enabled", True)}
    if sites:
        enabled &= set(sites)
    enabled -= set(skip_sites or ())
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


def run_scrape(cfg, sites=None, skip_sites=None) -> RunSummary:
    return scrape_companies(cfg, load_companies(cfg, sites, skip_sites))


def scrape_companies(cfg, companies: list[CompanyRef], *, workers: int | None = None,
                     deadline: float | None = None, client: PoliteClient | None = None,
                     run_id: str | None = None, watch: NetworkWatch | None = None) -> RunSummary:
    """Read exactly these boards. `deadline` (time.monotonic()) is a time budget: no board is started after
    it, and no rate-limit wait runs past it -- those boards are recorded not_attempted or rate_limited, never as
    read. Without one this is the full run as it always was (`run_scrape`)."""
    client = client or PoliteClient(HttpSettings.from_config(cfg))
    scrapers = build_scrapers(cfg, client)
    job_filter = resolve_filter(cfg)
    summary = RunSummary(run_id=run_id or uuid.uuid4().hex[:12])
    deactivate_after = int(cfg.get("limits.deactivate_after_failures", 5) or 0)
    close_on_empty = bool(cfg.get("limits.close_on_empty_board", False))

    workers = int(workers or cfg.get("limits.scrape_workers", 6) or 1)
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
    watch = watch or NetworkWatch(streak=int(cfg.get("limits.network_down_streak", 5) or 5))

    def walk(host_companies: list) -> None:
        for company in host_companies:
            if watch.down:
                with lock:
                    summary.outcomes[(company.source, company.slug)] = (NOT_ATTEMPTED, NETWORK_DOWN)
                continue
            if deadline is not None and time.monotonic() >= deadline:
                with lock:
                    summary.outcomes[(company.source, company.slug)] = (NOT_ATTEMPTED, "TimeBudget")
                continue
            _scrape_one(company, scrapers, job_filter, summary, lock,
                        deactivate_after, close_on_empty, watch=watch)

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

    max_wait = 20 * 60
    if deadline is not None:
        max_wait = max(0.0, min(max_wait, deadline - time.monotonic() - 60))
    if watch.down:
        max_wait = 0.0                    # no internet: no wait for a host to cool off either
    _retry_deferred(summary, scrapers, client, job_filter, lock, deactivate_after, close_on_empty, workers,
                    max_wait=max_wait, deadline=deadline, watch=watch)
    if watch.down:
        summary.network_down = True
        for key, (status, error) in list(summary.outcomes.items()):
            if status == FAILED and error == "NetworkError":
                summary.outcomes[key] = (NOT_ATTEMPTED, NETWORK_DOWN)    # failed only because nothing answered
    return summary


def scrape_boards(cfg, refs: list[CompanyRef]) -> RunSummary:
    """A few boards now, read exactly as the daily run reads them -- the same filter, the same
    politeness, the same bookkeeping. A board just put on the watch list shows its openings in
    minutes instead of after the next morning's run. One thread: these are a handful of boards."""
    client = PoliteClient(HttpSettings.from_config(cfg))
    scrapers = build_scrapers(cfg, client)
    job_filter = resolve_filter(cfg)
    summary = RunSummary(run_id=uuid.uuid4().hex[:12])
    deactivate_after = int(cfg.get("limits.deactivate_after_failures", 5) or 0)
    close_on_empty = bool(cfg.get("limits.close_on_empty_board", False))
    lock = threading.Lock()
    for ref in refs:
        _scrape_one(ref, scrapers, job_filter, summary, lock, deactivate_after, close_on_empty)
    return summary


def _retry_deferred(summary, scrapers, client, job_filter, lock, deactivate_after, close_on_empty,
                    workers, max_wait: float = 20 * 60, deadline: float | None = None,
                    watch: "NetworkWatch | None" = None) -> None:
    """Ask the boards a host turned away once more, after its cool-off ends."""
    if not summary.deferred:
        return
    again, summary.deferred = summary.deferred, []
    hosts: dict[str, list] = {}
    for company in again:
        hosts.setdefault(_host_of(scrapers[company.source], company), []).append(company)
    cooling = max(client.cooling_left(h) for h in hosts)
    if cooling > max_wait:
        # Not asked again inside this run's time: they stay rate-limited -- incomplete, never read.
        log.info("%d board(s) were rate-limited and their hosts cool off past this run's time; left as "
                 "rate-limited", len(again))
        for company in again:
            summary.outcomes[(company.source, company.slug)] = (RATE_LIMITED, "RateLimited")
        return
    wait = cooling
    log.info("%d board(s) were rate-limited; asking again in %.0f min", len(again), wait / 60)
    time.sleep(wait)

    def walk(host_companies: list) -> None:
        for company in host_companies:
            if (deadline is not None and time.monotonic() >= deadline) or (watch is not None and watch.down):
                with lock:          # its time ran out before it was asked again: still rate-limited, not read
                    summary.outcomes[(company.source, company.slug)] = (RATE_LIMITED, "RateLimited")
                continue
            _scrape_one(company, scrapers, job_filter, summary, lock,
                        deactivate_after, close_on_empty, retrying=True, watch=watch)

    if workers <= 1:
        for group in hosts.values():
            walk(group)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(walk, hosts.values()))


def _is_in_memory() -> bool:
    """Is the connected database one that cannot be shared between threads?"""
    try:
        from data_engineering.db import session as session_mod

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
                deactivate_after, close_on_empty, retrying: bool = False, watch: NetworkWatch | None = None) -> None:
    """One board, start to finish. Runs on a worker thread."""
    scraper = scrapers.get(company.source)
    if scraper is None:
        return

    with lock:
        summary.companies_attempted += 1
    started = time.monotonic()
    try:
        got = _fetch(scraper, company, job_filter)
    except RobotsDisallowed as exc:
        if watch is not None:
            watch.answered()
        with lock:
            _log_failure(summary, company, "RobotsDisallowed", str(exc), started,
                         deactivate_after)
            summary.outcomes[(company.source, company.slug)] = (FAILED, "RobotsDisallowed")
        return
    except RateLimited as exc:
        if watch is not None:
            watch.answered()               # a refusal is an answer: the network is there
        with lock:
            summary.outcomes[(company.source, company.slug)] = (RATE_LIMITED, "RateLimited")
            if not retrying:
                # Not a failure yet: asked again once the host has cooled off.
                summary.companies_attempted -= 1
                summary.deferred.append(company)
            else:
                _log_failure(summary, company, "RateLimited", str(exc), started, deactivate_after)
        return
    except Exception as exc:
        silent = isinstance(exc, NetworkError)
        if watch is not None:
            if silent:
                watch.silent()
            else:
                watch.answered()
        with lock:
            _log_failure(summary, company, type(exc).__name__, str(exc), started,
                         deactivate_after)
            summary.outcomes[(company.source, company.slug)] = (FAILED, type(exc).__name__)
        return
    if watch is not None:
        watch.answered()

    raw_jobs = got.jobs
    kept = job_filter.apply(raw_jobs)

    # One writer at a time past this point. SQLite takes an exclusive lock for
    # a write, so letting six threads insert at once buys nothing and produces
    # "database is locked" instead. The slow part — the network — already
    # happened above, outside the lock.
    with lock:
        new_count, updated_count = _persist(kept)

        # Only reachable on a SUCCESSFUL fetch — every failure path above has
        # already returned, so an outage can never close a company's jobs.
        # Reconciled against EVERY posting the listing named, not the filtered
        # subset: a stored job whose title no longer matches the filters, or
        # whose description was never asked for, is still on the board
        # (data_engineering/scraper/lifecycle.py). A listing that is not
        # complete, or whose ids are not the stored ones, closes nothing.
        if got.listed_ids is not None:
            lifecycle = reconcile_board(company.source, company.slug, got.listed_ids,
                                        close_on_empty_board=close_on_empty, partial=False)
        else:
            lifecycle = reconcile_board(company.source, company.slug, got.seen_ids,
                                        close_on_empty_board=close_on_empty, partial=True)

        summary.jobs_seen += got.detail["listed"]
        summary.jobs_kept += len(kept)
        summary.jobs_new += new_count
        summary.jobs_updated += updated_count
        summary.jobs_closed += lifecycle.closed
        summary.jobs_reopened += lifecycle.reopened

        _log_success(summary, company, got.detail["listed"], len(kept), new_count, started)
        done = got.detail["listing"] == "complete" and got.detail["pending"] == 0
        summary.outcomes[(company.source, company.slug)] = (COMPLETE if done else PARTIAL, "")
        summary.details[(company.source, company.slug)] = dict(got.detail)

    log.info(
        "%-11s %-24s %3d seen  %3d match  %3d new  %3d closed%s",
        company.source, company.slug, got.detail["listed"], len(kept), new_count,
        lifecycle.closed,
        (f"  ({got.detail['pending']} description(s) pending)" if got.detail["pending"] else "")
        + ("  (listing incomplete)" if got.detail["listing"] != "complete" else ""),
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
                        stated_job_type=raw.job_type,
                        **derive(raw.title, raw.location,
                                 raw.description, raw.requirements, raw.job_type),
                    )
                )
                new_count += 1
            elif raw.detail_missing:
                # Its description did not come this time: the posting is still listed, and its stored text
                # stays as it was rather than being wiped by an empty one.
                continue
            elif existing.content_hash != content_hash:
                # Posting was edited — refresh in place, never duplicate.
                existing.title = raw.title
                existing.location = raw.location
                existing.description = raw.description
                existing.requirements = raw.requirements
                existing.application_url = raw.application_url
                existing.content_hash = content_hash
                existing.stated_job_type = raw.job_type or existing.stated_job_type
                from backend.core.jobfields import derived_for
                for field, value in derived_for(existing, derive(raw.title, raw.location,
                                                raw.description, raw.requirements,
                                                existing.stated_job_type)).items():
                    setattr(existing, field, value)
                existing.exported_to_excel = False  # re-export in Phase 2
                updated_count += 1
            elif raw.job_type and raw.job_type != existing.stated_job_type:
                # The text is the same, but the board now says what kind of job it is (or says
                # it differently): weigh that, unless the person has set it themselves.
                from backend.core.jobfields import job_type
                existing.stated_job_type = raw.job_type
                if not existing.job_type_manual:
                    existing.job_type = job_type(existing.title, existing.description,
                                                 existing.requirements, raw.job_type)

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


@dataclass
class Fetched:
    """One board read: what to store, and what may be judged from it.

    jobs        the postings to store (described; or, where listing ids are durable, listed with a missing
                description -- kept, their stored text untouched);
    listed_ids  every id a COMPLETE listing with durable ids named: closure is judged against these. None: this
                read may close nothing (an incomplete listing, or ids that are not the stored ones);
    seen_ids    the stored postings this read saw, refreshed when it may close nothing;
    detail      the board at the posting level (RunSummary.details)."""
    jobs: list
    listed_ids: list | None
    seen_ids: list
    detail: dict


def _known(scraper, company) -> dict:
    """{listing id: stored id} for the board's postings already stored, on a site whose listing ids are not the
    stored ones (Workday: the URL path, against the requisition id): how a read knows which listed postings it
    has described before. Read-only."""
    key_of = getattr(scraper, "listing_key", None)
    if key_of is None:
        return {}
    try:
        with get_session() as session:
            rows = (session.query(Job.application_url, Job.external_id)
                    .filter_by(source=company.source, company_slug=company.slug).all())
    except RuntimeError:            # no database opened (a reader used on its own): nothing is known yet
        return {}
    known = {}
    for url, external_id in rows:
        key = key_of(url)
        if key:
            known[key] = external_id
        if str(external_id).startswith("/job/"):
            known[str(external_id)] = external_id      # stored before its requisition id was known
    return known


def _fetch(scraper, company, job_filter) -> Fetched:
    """Every posting worth keeping from one board, and how much of the board was seen.

    Most boards hand over titles, locations and descriptions together, so there
    is nothing to decide: fetch, then filter. That listing is the whole board.

    Workday, Workable, BambooHR, JazzHR and Jobvite do not: the listing carries
    no descriptions and each one costs a separate request, so the scraper splits
    the work into `stub_jobs` and `fill_details`, and the title and location
    filter runs in between. Only the postings the search wants are described;
    the rest are skipped on purpose -- which says nothing about whether the
    board was read. Until Nov 2026 it did: a skipped posting made the board
    "partial", and a partial board closes nothing, so on JazzHR and BambooHR a
    posting taken down was never closed (on JazzHR, half the open postings
    went unseen for weeks).

    Now three things are kept apart:
      * whether the LISTING is complete (`Listing.complete`: no page cap hit);
      * whether every RELEVANT posting has its description (`pending`);
      * how many were skipped as irrelevant (`skipped`) -- never a shortfall.
    A complete listing whose ids are the stored ones (`durable_listing_ids`)
    is authoritative: closure is judged against everything it names. Workday's
    are not (a URL path, where the stored id is the requisition id), so Workday
    still closes nothing; instead the postings it has described before are
    recognised by their path, and the ones never described are asked for first,
    so the next read resumes where this one stopped.
    """
    stubs = getattr(scraper, "stub_jobs", None)
    if stubs is None:
        jobs = scraper.fetch_jobs(company)
        ids = [raw.external_id for raw in jobs]
        return Fetched(jobs, ids, ids, {"listing": "complete", "listed": len(jobs), "relevant": len(jobs),
                                        "described": len(jobs), "pending": 0, "skipped": 0})

    listed = stubs(company)
    complete = bool(getattr(listed, "complete", True))
    candidates = [job for job in listed if job_filter.matches_stub(job)]
    durable = bool(getattr(scraper, "durable_listing_ids", False))
    known = {} if durable else _known(scraper, company)
    unknown = [job for job in candidates if job.external_id not in known]
    first = {id(job) for job in unknown}
    ordered = unknown + [job for job in candidates if id(job) not in first]
    filled = scraper.fill_details(company, ordered)

    if len(listed) > len(candidates):
        log.info("%s: %d of %d postings worth a description", company.name,
                 len(candidates), len(listed))
    detail = {"listing": "complete" if complete else "incomplete", "listed": len(listed),
              "relevant": len(candidates), "skipped": len(listed) - len(candidates)}
    if complete and getattr(listed, "total", None) and listed.total > len(listed) and not durable:
        detail["listing"] = "incomplete"
    if durable:
        missing = sum(1 for job in filled if job.detail_missing)
        detail.update(described=len(filled) - missing, pending=missing + max(0, len(candidates) - len(filled)))
        ids = [job.external_id for job in listed]
        return Fetched(filled, ids if complete else None, ids, detail)

    described = [job for job in filled if not job.detail_missing]
    got_unknown = sum(1 for job in described if id(job) in first)
    detail.update(described=len(described), pending=len(unknown) - got_unknown)
    seen = [job.external_id for job in described]
    seen += [known[job.external_id] for job in listed if job.external_id in known]
    return Fetched(described, None, list(dict.fromkeys(seen)), detail)


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
        # A host refusing us says nothing about the board: it is not counted
        # toward retiring it, or one busy afternoon retires healthy boards.
        # Nor is no answer at all (NetworkError: no name lookup, no connection,
        # a timeout) -- an outage once retired healthy boards that way. A
        # board is retired only on what its host said.
        if row is not None and error_type not in ("RateLimited", "NetworkError"):
            row.consecutive_failures += 1
            # A dead slug shouldn't be re-probed forever once the list is large.
            if deactivate_after and row.consecutive_failures >= deactivate_after:
                row.active = False
                log.warning(
                    "Deactivating %s/%s after %d consecutive failures",
                    company.source, company.slug, row.consecutive_failures,
                )
