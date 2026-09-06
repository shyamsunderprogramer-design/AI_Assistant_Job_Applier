#!/usr/bin/env python3
"""Job Applier Agent CLI. Phase 1: discovery + scraping.

    python main.py init-db
    python main.py scrape
    python main.py discover --names companies.txt
    python main.py import-csv --file ats_companies.csv
    python main.py export
    python main.py stats
    python main.py failures
"""

from __future__ import annotations

import argparse
import logging
import sys

from config.loader import load_config, setup_logging
from db.models import Company, Job, ScrapeLog
from db.session import get_session, init_engine

log = logging.getLogger("main")


def cmd_init_db(cfg, args) -> int:
    init_engine(cfg.database_url)
    from scraper.runner import sync_seed_companies

    count = sync_seed_companies(cfg)
    print(f"Database ready at {cfg.database_url}")
    print(f"Synced {count} seed companies from config.yaml")
    return 0


def cmd_scrape(cfg, args) -> int:
    init_engine(cfg.database_url)
    from scraper.runner import run_scrape, sync_seed_companies

    sync_seed_companies(cfg)
    summary = run_scrape(cfg)

    print()
    print(f"Run {summary.run_id}")
    print(f"  Companies scraped : {summary.companies_attempted} "
          f"({summary.companies_failed} failed)")
    print(f"  Jobs seen         : {summary.jobs_seen}")
    print(f"  Matched filters   : {summary.jobs_kept}")
    print(f"  New in DB         : {summary.jobs_new}")
    print(f"  Updated postings  : {summary.jobs_updated}")
    print(f"  Closed (gone)     : {summary.jobs_closed}")
    print(f"  Reopened          : {summary.jobs_reopened}")

    if summary.failures:
        print("\n  Failures:")
        for source, slug, error in summary.failures:
            print(f"    {source}/{slug}: {error}")
    return 0


def cmd_discover(cfg, args) -> int:
    init_engine(cfg.database_url)
    from scraper.discovery import CompanyDiscoverer, load_names_from_file
    from scraper.http_client import HttpSettings, PoliteClient
    from scraper.runner import build_scrapers

    names = load_names_from_file(args.names)
    if args.limit:
        names = names[: args.limit]
    print(f"Probing {len(names)} company names against "
          f"{', '.join(cfg.get('discovery.probe_sources', []))}...")

    client = PoliteClient(HttpSettings.from_config(cfg))
    discoverer = CompanyDiscoverer(
        scrapers=build_scrapers(cfg, client),
        strip_suffixes=cfg.get("discovery.strip_suffixes", []),
    )
    results = discoverer.discover_from_names(names, cfg.get("discovery.probe_sources"))

    found = [r for r in results if r.found]
    print(f"\nFound {len(found)} of {len(results)} on Greenhouse/Lever:")
    for r in found:
        print(f"  {r.name:<34} {r.source:<11} {r.slug}")
    return 0


def cmd_import_csv(cfg, args) -> int:
    init_engine(cfg.database_url)
    from scraper.discovery import import_inventory_csv

    count = import_inventory_csv(args.file)
    print(f"Imported {count} companies from {args.file}")
    return 0


def cmd_export(cfg, args) -> int:
    init_engine(cfg.database_url)
    from excel.tracker import export_jobs

    appended, updated, total = export_jobs(cfg, only_new=not args.all)
    path = cfg.get("excel.path", "data/job_tracker.xlsx")
    if total == 0:
        print("Nothing to export — no unexported jobs. Use --all to re-export everything.")
        return 0
    print(f"Exported {total} jobs to {path}")
    print(f"  Appended : {appended}")
    print(f"  Updated  : {updated}")
    return 0


def cmd_reparse(cfg, args) -> int:
    """Re-derive the requirements section from stored JDs (no network calls).

    Useful after the extraction heuristic improves — the stored description is
    the source, so nothing needs re-scraping.
    """
    init_engine(cfg.database_url)
    from scraper.base import extract_requirements

    changed = 0
    with get_session() as session:
        jobs = session.query(Job).all()
        for job in jobs:
            fresh = extract_requirements(job.description)
            if fresh != job.requirements:
                job.requirements = fresh
                job.exported_to_excel = False
                changed += 1
    print(f"Re-derived requirements for {changed} of {len(jobs)} jobs.")
    return 0


def cmd_score(cfg, args) -> int:
    init_engine(cfg.database_url)
    from resume.pipeline import score_jobs

    outcomes = score_jobs(
        cfg, limit=args.limit, rescore=args.rescore, include_closed=args.include_closed
    )
    if not outcomes:
        print("Nothing to score. Use --rescore to recompute existing scores.")
        return 0

    threshold = float(cfg.get("resume.min_score", 0.45))
    outcomes.sort(key=lambda o: o.score, reverse=True)
    print(f"Scored {len(outcomes)} jobs (threshold {threshold:.0%}):\n")
    print(f"{'score':>6}  {'company':<20} title")
    for outcome in outcomes[: args.top or len(outcomes)]:
        flag = " " if outcome.score >= threshold else "!"
        print(f"{outcome.score:>6.0%}{flag} {outcome.company:<20} {outcome.title[:52]}")

    below = sum(1 for o in outcomes if o.score < threshold)
    print(f"\n{below} below threshold — marked 'Manual Review'.")
    print("Run `export` to push scores into the Excel sheet.")
    return 0


def cmd_tailor(cfg, args) -> int:
    init_engine(cfg.database_url)
    from resume.pipeline import tailor_jobs

    job_ids = [int(i) for i in args.job_id] if args.job_id else None
    outcomes = tailor_jobs(
        cfg, job_ids=job_ids, limit=args.limit, include_closed=args.include_closed
    )
    if not outcomes:
        print("No jobs eligible for tailoring. Run `score` first, or pass --job-id.")
        return 0

    for outcome in outcomes:
        print(f"[{outcome.status:<16}] {outcome.company} — {outcome.title[:44]}")
        if outcome.detail:
            print(f"                    {outcome.detail[:100]}")
        if outcome.resume_path:
            print(f"                    -> {outcome.resume_path}")

    rejected = [o for o in outcomes if o.status == "rejected"]
    if rejected:
        print(f"\n{len(rejected)} tailoring(s) REJECTED by the fabrication guard "
              f"and flagged for manual review.")
    return 0


def cmd_stats(cfg, args) -> int:
    init_engine(cfg.database_url)
    from statistics import median

    from db.models import utcnow
    from scraper.lifecycle import as_utc, stale_companies

    with get_session() as session:
        companies = session.query(Company).count()
        jobs = session.query(Job).count()
        open_jobs = session.query(Job).filter(Job.is_open.is_(True)).count()
        by_source = {}
        for source, in session.query(Job.source).distinct():
            by_source[source] = session.query(Job).filter_by(source=source).count()
        recent = (
            session.query(Job)
            .filter(Job.is_open.is_(True))
            .order_by(Job.found_at.desc())
            .limit(10)
            .all()
        )
        # Age of what is actually live. posted_at is missing on some boards,
        # so fall back to when we first saw it — never silently drop the row.
        now = utcnow()
        ages = [
            (now - as_utc(job.posted_at or job.found_at)).days
            for job in session.query(Job).filter(Job.is_open.is_(True)).all()
            if (job.posted_at or job.found_at) is not None
        ]

    print(f"Companies tracked : {companies}")
    print(f"Jobs stored       : {jobs}")
    print(f"  open            : {open_jobs}")
    print(f"  closed          : {jobs - open_jobs}")
    for source, count in by_source.items():
        print(f"  {source:<14}: {count}")
    if ages:
        print(f"Median age (open) : {median(ages):.0f} days")

    warn_days = int(cfg.get("limits.stale_company_warn_days", 7) or 0)
    stale = stale_companies(warn_days)
    if stale:
        # Silence is the failure mode: a company that stopped being scraped
        # keeps every job marked open forever, because closure only happens on
        # a successful fetch. Nothing errors — the rows just quietly rot.
        print(f"\nStale — not scraped in {warn_days}+ days ({len(stale)}):")
        for row in stale[:10]:
            when = "never" if row.days is None else f"{row.days}d ago"
            print(f"  {row.source:<11} {row.slug:<24} {when}")
        if len(stale) > 10:
            print(f"  ... and {len(stale) - 10} more")

    if recent:
        print("\nMost recent open finds:")
        for job in recent:
            print(f"  {job.company:<20} {job.title[:56]}")
    return 0


def cmd_failures(cfg, args) -> int:
    init_engine(cfg.database_url)
    with get_session() as session:
        rows = (
            session.query(ScrapeLog)
            .filter(ScrapeLog.ok.is_(False))
            .order_by(ScrapeLog.created_at.desc())
            .limit(args.limit)
            .all()
        )
    if not rows:
        print("No scrape failures logged.")
        return 0
    print(f"{'source':<11} {'company':<24} error")
    for row in rows:
        print(f"{row.source:<11} {row.company_slug:<24} {row.error_type}: "
              f"{(row.error_message or '')[:80]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Job Applier Agent")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create tables and sync seed companies")
    sub.add_parser("scrape", help="Scrape all configured/discovered companies")

    p_discover = sub.add_parser("discover", help="Probe company names for ATS boards")
    p_discover.add_argument("--names", required=True, help="A .txt (one name per line) or .csv")
    p_discover.add_argument("--limit", type=int, default=0, help="Only probe the first N names")

    p_import = sub.add_parser("import-csv", help="Import an inventory CSV (name,slug,source)")
    p_import.add_argument("--file", required=True)

    p_export = sub.add_parser("export", help="Write discovered jobs to the Excel tracker")
    p_export.add_argument(
        "--all", action="store_true", help="Re-export every job, not just new ones"
    )

    sub.add_parser("reparse", help="Re-derive requirements from stored JDs (no network)")

    p_score = sub.add_parser("score", help="Score jobs against the base resume (no API cost)")
    p_score.add_argument("--limit", type=int, default=0, help="Only score N jobs")
    p_score.add_argument("--rescore", action="store_true", help="Recompute existing scores")
    p_score.add_argument("--top", type=int, default=25, help="Rows to print")
    p_score.add_argument(
        "--include-closed", action="store_true", help="Also score postings no longer on the board"
    )

    p_tailor = sub.add_parser("tailor", help="Tailor the resume per job via the Claude API")
    p_tailor.add_argument("--job-id", action="append", help="Tailor specific job id(s)")
    p_tailor.add_argument("--limit", type=int, default=0, help="Cap how many jobs to tailor")
    p_tailor.add_argument(
        "--include-closed", action="store_true", help="Also tailor for closed postings"
    )

    sub.add_parser("stats", help="Show DB counts and recent finds")

    p_failures = sub.add_parser("failures", help="Show logged scrape failures")
    p_failures.add_argument("--limit", type=int, default=30)

    return parser


COMMANDS = {
    "init-db": cmd_init_db,
    "scrape": cmd_scrape,
    "discover": cmd_discover,
    "import-csv": cmd_import_csv,
    "export": cmd_export,
    "reparse": cmd_reparse,
    "score": cmd_score,
    "tailor": cmd_tailor,
    "stats": cmd_stats,
    "failures": cmd_failures,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config) if args.config else load_config()
    setup_logging(cfg)
    return COMMANDS[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
