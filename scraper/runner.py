"""Orchestrates a scrape run across every configured/discovered company.

A failing company is logged to scrape_log and the run continues — one broken
board never aborts the batch (README.md §C6).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

from db.models import Company, Job, ScrapeLog, utcnow
from db.session import get_session
from scraper.ashby import AshbyScraper
from scraper.base import CompanyRef, PortalScraper, RawJob
from scraper.discovery import upsert_company
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
    log.info("Run %s — scraping %d companies", summary.run_id, len(companies))

    for company in companies:
        scraper = scrapers.get(company.source)
        if scraper is None:
            continue

        summary.companies_attempted += 1
        started = time.monotonic()
        try:
            raw_jobs = scraper.fetch_jobs(company)
        except RobotsDisallowed as exc:
            _log_failure(summary, company, "RobotsDisallowed", str(exc), started, deactivate_after)
            continue
        except Exception as exc:
            _log_failure(summary, company, type(exc).__name__, str(exc), started, deactivate_after)
            continue

        kept = job_filter.apply(raw_jobs)
        new_count, updated_count = _persist(kept)

        # Only reachable on a SUCCESSFUL fetch — every failure path above has
        # already `continue`d, so an outage can never close a company's jobs.
        # Reconciled against EVERY posting returned, not the filtered subset:
        # a stored job whose title no longer matches the filters is still on
        # the board (scraper/lifecycle.py).
        lifecycle = reconcile_board(
            company.source,
            company.slug,
            [raw.external_id for raw in raw_jobs],
            close_on_empty_board=close_on_empty,
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

    return summary


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
