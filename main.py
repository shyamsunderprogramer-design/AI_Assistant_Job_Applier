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
from db.models import SYSTEM_STATUSES, Company, Job, ScrapeLog
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
    sources = cfg.get("discovery.probe_sources", [])

    client = PoliteClient(HttpSettings.from_config(cfg))
    discoverer = CompanyDiscoverer(
        scrapers=build_scrapers(cfg, client),
        strip_suffixes=cfg.get("discovery.strip_suffixes", []),
    )

    if args.dry_run:
        # Discovery spends a request per GUESS, so show what a run would cost
        # before it is spent (README.md §C5).
        pairs = []
        for raw in names:
            nm, _, dom = str(raw).partition(",")
            pairs.extend(discoverer.plan(nm.strip(), sources, domain=dom.strip() or None,
                                         max_slugs=args.max_slugs))
        delay = float(cfg.get("http.min_delay_seconds", 1.5))
        print(f"{len(names)} names -> {len(pairs)} probes across {', '.join(sources)}")
        print(f"Roughly {len(pairs) * delay / 60:.0f} minutes at the configured "
              f"{delay}s per-host delay (minus anything already cached).\n")
        for raw in names[:10]:
            nm, _, dom = str(raw).partition(",")
            planned = ", ".join(
                f"{s}/{sl}" for s, sl in discoverer.plan(
                    nm.strip(), sources, domain=dom.strip() or None, max_slugs=args.max_slugs)
            )
            print(f"  {nm.strip():<30} {planned}")
        if len(names) > 10:
            print(f"  ... and {len(names) - 10} more names")
        return 0

    print(f"Probing {len(names)} company names against {', '.join(sources)}...")
    print("Safe to interrupt — every result is saved as it is found.\n")

    def report(progress, result):
        if result.found:
            print(f"  [{progress.found:>4}] {result.name:<30} {result.source:<11} {result.slug}")
        elif progress.names % 50 == 0:
            print(f"  ... {progress.names}/{len(names)} names, {progress.found} found")

    results = discoverer.discover_from_names(
        names, sources, on_progress=report, max_slugs=args.max_slugs
    )

    found = [r for r in results if r.found]
    print(f"\n{discoverer.progress.summary()}")
    by_source: dict[str, int] = {}
    for r in found:
        by_source[r.source] = by_source.get(r.source, 0) + 1
    for source, count in sorted(by_source.items()):
        print(f"  {source:<12}: {count}")
    if found:
        print("\nNext: python main.py scrape")
    return 0


SETUP_HELP = """
No mailbox credentials found. Two ways in — pick either.

  A. GMAIL APP PASSWORD  (2 minutes, read-only)
     1. Turn on 2-Step Verification:  https://myaccount.google.com/security
     2. Create an app password:       https://myaccount.google.com/apppasswords
        Name it anything, e.g. "job applier". Google shows 16 characters once.
     3. Put both lines in .env (it is gitignored — the key never leaves this machine):

          MAIL_ADDRESS=you@gmail.com
          MAIL_APP_PASSWORD=abcd efgh ijkl mnop

     4. python main.py scan-mail

     The connection is opened READ-ONLY, so nothing can be deleted, moved, or
     marked read. Revoke the password any time on that same Google page.

  B. NO CREDENTIALS AT ALL
     Export your mail at https://takeout.google.com (Mail only), then:

          python main.py scan-mail --mbox ~/Downloads/All\\ mail.mbox

     Slower to obtain, but nothing leaves your machine.
"""


def cmd_scan_mail(cfg, args) -> int:
    """Harvest company names from a mailbox. Read-only."""
    import os

    from scraper.mailbox import scan_imap, scan_mbox, write_findings

    if args.mbox:
        print(f"Scanning {args.mbox} — offline, nothing leaves this machine.\n")
        findings = scan_mbox(args.mbox, limit=args.limit)
    else:
        address = args.address or os.getenv("MAIL_ADDRESS")
        password = args.password or os.getenv("MAIL_APP_PASSWORD")
        if not address or not password:
            print(SETUP_HELP)
            return 1

        password = password.replace(" ", "")  # Google prints it in groups of 4
        print(f"Connecting to {args.host} as {address} (read-only)...")
        try:
            findings = scan_imap(
                address, password, host=args.host, folder=args.folder,
                since=args.since, limit=args.limit,
                progress=lambda i, n, f: print(
                    f"  {i}/{n} messages — {len(f.names)} names, "
                    f"{len(f.ats_slugs)} boards so far"
                ),
            )
        except Exception as exc:
            message = str(exc)
            print(f"\nCould not read the mailbox: {message}")
            if "AUTHENTICATIONFAILED" in message.upper() or "Invalid credentials" in message:
                print("\nThat usually means the app password is wrong, or a normal")
                print("account password was used. App passwords are 16 characters and")
                print("come from https://myaccount.google.com/apppasswords")
            return 1

    print(f"\n{findings.summary()}")

    if findings.ats_slugs:
        print(f"\nConfirmed ATS boards ({len(findings.ats_slugs)}) — these are slugs, not guesses:")
        for slug, source in sorted(findings.ats_slugs.items())[:20]:
            print(f"  {source:<11} {slug}")
        if len(findings.ats_slugs) > 20:
            print(f"  ... and {len(findings.ats_slugs) - 20} more")

    ranked = findings.ranked_names(args.min_mentions)
    if ranked:
        print(f"\nTop company names ({len(ranked)} total):")
        for name, count in ranked[:20]:
            print(f"  {count:>3}x  {name}")

    if not ranked and not findings.ats_slugs:
        print("\nNothing found. Try --since 01-Jan-2025, a different --folder, "
              "or lower --min-mentions.")
        return 0

    path, written = write_findings(findings, args.out, args.min_mentions)
    print(f"\nWrote {written} names to {path}")

    if findings.ats_slugs and not args.no_save_boards:
        init_engine(cfg.database_url)
        from scraper.discovery import record_probe, upsert_company
        from scraper.runner import SCRAPER_TYPES

        added = 0
        for slug, source in findings.ats_slugs.items():
            if source not in SCRAPER_TYPES:
                continue
            upsert_company(name=slug, slug=slug, source=source, origin="mailbox")
            record_probe(source, slug, True, company_name=slug)
            added += 1
        print(f"Added {added} confirmed boards straight to the company list "
              f"(no probing needed — the URL named the slug).")

    print(f"\nNext: python main.py discover --names {path} --max-slugs 2")
    return 0


def cmd_import_names(cfg, args) -> int:
    """Turn a big company export into a filtered discovery list."""
    from scraper.company_import import read_spreadsheet, write_names_file

    records = read_spreadsheet(
        args.file,
        sheet=args.sheet,
        countries=tuple(c.strip().lower() for c in args.countries.split(",")) if args.countries else (),
        min_employees=args.min_employees,
        max_employees=args.max_employees,
        limit=args.limit,
    )
    if not records:
        print("Nothing matched those filters.")
        return 1

    out = write_names_file(records, args.out)
    with_domain = sum(1 for r in records if r.slug_hint())
    print(f"{len(records):,} companies -> {out}")
    print(f"  with a usable domain : {with_domain:,} ({with_domain * 100 // len(records)}%)")
    print(f"  headcount band       : {args.min_employees}–{args.max_employees}")
    print("\n  Largest first:")
    for r in records[:8]:
        print(f"    {r.name[:34]:36} {r.slug_hint() or '(no domain)':22} {r.employees:>7,}")
    print(f"\nNext: python main.py discover --names {out} --dry-run")
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


def cmd_prune(cfg, args) -> int:
    """Re-apply the current search to jobs already stored.

    Filters are only applied at scrape time, so the DB accumulates every job
    that matched any filter generation ever used. After narrowing a search —
    or deriving a new one from a resume — the old jobs stay, get ranked, and
    get recommended. That is the same rot Phase 5 fixed for closed postings.
    """
    init_engine(cfg.database_url)
    from scraper.base import RawJob
    from scraper.filters import resolve_filter

    job_filter = resolve_filter(cfg)
    stale: list[tuple[Job, str]] = []

    with get_session() as session:
        for job in session.query(Job).all():
            if job.status not in SYSTEM_STATUSES:
                continue  # the user has acted on this one; it is theirs to keep
            reason = job_filter.reject_reason(
                RawJob(
                    source=job.source, company=job.company, company_slug=job.company_slug,
                    external_id=job.external_id, title=job.title, location=job.location,
                    description=job.description, application_url=job.application_url,
                )
            )
            if reason:
                stale.append((job, reason))

        if not stale:
            print("Every stored job still matches the current search.")
            return 0

        from collections import Counter
        counts = Counter(reason for _, reason in stale)
        print(f"{len(stale)} stored jobs no longer match the current search:")
        for reason, count in counts.most_common():
            print(f"  {count:>4}  {reason}")

        if not args.apply:
            print("\nShowing the first 10:")
            for job, reason in stale[:10]:
                print(f"  {job.company:<18} {job.title[:44]:46} {job.location or ''}")
            print("\nRe-run with --apply to remove them. Jobs you have already "
                  "acted on (Applied, Rejected, ...) are never touched.")
            return 0

        from excel.tracker import ExcelTracker, job_key

        # Take the keys before deleting — the objects are unusable afterwards.
        keys = {job_key(job) for job, _ in stale}
        for job, _ in stale:
            session.delete(job)

        # The sheet is append-only, so a pruned job's row would otherwise
        # linger as a dead entry the user still clicks on.
        removed_rows = ExcelTracker(cfg.get("excel.path", "data/job_tracker.xlsx")).remove(keys)
        print(f"\nRemoved {len(stale)} jobs and {removed_rows} sheet rows.")
        if removed_rows < len(stale):
            print(f"  {len(stale) - removed_rows} row(s) kept — you had already "
                  f"acted on them, or they were never exported.")
    return 0


def cmd_profile(cfg, args) -> int:
    """Derive the job search from the resume — no keywords to hand-write."""
    from config.loader import PROJECT_ROOT
    from resume.pipeline import load_base_resume
    from resume.profile import PROFILE_FILENAME, derive_search_profile, save_profile

    path = PROJECT_ROOT / cfg.get("filters.profile_path", PROFILE_FILENAME)
    if path.exists() and not args.force and not args.show:
        print(f"A search profile already exists at {path.name}.")
        print("  --show   print it     --force  regenerate from the resume (overwrites edits)")
        return 0

    resume = load_base_resume(cfg)
    profile = derive_search_profile(resume)

    years = f"{profile.years_experience:.0f}" if profile.years_experience else "unknown"
    print(f"Resume     : {profile.resume_path}")
    print(f"Read as    : {profile.seniority} level, {years} years of experience")
    print(f"Titles held: {', '.join(profile.held_titles[:6]) or 'none detected'}")
    print(f"Searching  : {', '.join(profile.families) or 'no role family matched'}")
    if profile.considered_families:
        print(f"Considered : {', '.join(profile.considered_families)} — too weak in your "
              f"resume to search for; add them to the file if you disagree")
    print()
    print(f"  Job titles ({len(profile.titles)}):")
    for term in profile.titles:
        print(f"    + {term}")
    print(f"  Excluding ({len(profile.exclude_titles)}):")
    print(f"    - {', '.join(profile.exclude_titles)}")
    print(f"  Locations : {', '.join(profile.locations)}")

    if args.show:
        return 0

    save_profile(profile, path)
    print(f"\nSaved to {path.relative_to(PROJECT_ROOT)} — edit it freely, the scraper reads it.")
    print("Next: python main.py scrape")
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
    from resume.pipeline import estimate_tailoring, tailor_jobs

    job_ids = [int(i) for i in args.job_id] if args.job_id else None

    if args.estimate:
        est = estimate_tailoring(cfg, job_ids=job_ids, limit=args.limit)
        if not est["jobs"]:
            print("No jobs eligible for tailoring.")
            return 0
        print(f"{len(est['jobs'])} job(s) would be tailored with {est['model']}:\n")
        for job_id, company, title, usd in est["jobs"][:15]:
            print(f"  ~${usd:>6.3f}  [{job_id:>4}] {company:<20} {title[:44]}")
        if len(est["jobs"]) > 15:
            print(f"  ... and {len(est['jobs']) - 15} more")
        print(f"\nEstimated total: ~${est['total']:.2f}")
        if est["cap"] > 0:
            print(f"Run cap        : ${est['cap']:.2f}"
                  + ("  — the run would stop early" if est["total"] > est["cap"] else ""))
        else:
            print("Run cap        : none (resume.max_spend_per_run_usd is 0)")
        print("\nEstimates assume a full-length response; real cost is usually lower.")
        return 0
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

    cost = getattr(tailor_jobs, "last_run_cost", None)
    if cost and cost.calls:
        print(f"\nCost: ${cost.spent:.4f} across {cost.calls} call(s)"
              + (f" of a ${cost.cap:.2f} cap" if cost.cap else ""))
    if cost and cost.stopped_early:
        print("Stopped early — the next call would have breached the cap. "
              "Raise resume.max_spend_per_run_usd to continue.")
    return 0


def cmd_brief(cfg, args) -> int:
    """Write a paste-anywhere tailoring prompt. No API key, no cost."""
    init_engine(cfg.database_url)
    from resume.pipeline import brief_job

    try:
        path, company, title = brief_job(cfg, int(args.job_id))
    except LookupError as exc:
        print(exc)
        return 1

    print(f"Brief written for {company} — {title}")
    print(f"  {path}")
    print()
    print("  1. Open that file and copy everything below 'COPY FROM HERE'")
    print("  2. Paste it into Claude, ChatGPT, or any local model")
    print("  3. Save the JSON reply to a file")
    print(f"  4. python main.py accept {args.job_id} --file <that file>")
    print()
    print("The reply is checked for fabrication before anything is written.")
    return 0


def cmd_accept(cfg, args) -> int:
    """Take a pasted model reply, guard it, and write the resume."""
    init_engine(cfg.database_url)
    from pathlib import Path

    from resume.pipeline import accept_reply

    text = Path(args.file).read_text(encoding="utf-8")
    try:
        outcome = accept_reply(cfg, int(args.job_id), text)
    except (LookupError, ValueError) as exc:
        print(f"Could not use that reply: {exc}")
        return 1

    print(f"[{outcome.status}] {outcome.company} — {outcome.title}")
    if outcome.status == "rejected":
        print("\nREJECTED by the fabrication guard — nothing was written.")
        print(outcome.detail)
        print("\nThe model claimed something your resume does not support. "
              "Re-run the brief, or fix the reply by hand and try again.")
        return 1

    print(f"  {outcome.detail}")
    print(f"  -> {outcome.resume_path}")
    print(f"\nMatch score is now {outcome.score:.0%}. Run `export` to update the sheet.")
    return 0


def cmd_daily(cfg, args) -> int:
    """Everything that should happen once a day, in one command."""
    init_engine(cfg.database_url)
    from pathlib import Path

    from config.loader import PROJECT_ROOT
    from daily import render_digest, run_daily

    report = run_daily(cfg, skip_scrape=args.no_scrape, since_hours=args.since_hours)
    digest = render_digest(report, cfg, top=args.top)
    print()
    print(digest)

    # Writing the digest matters for a cron run, where stdout goes nowhere a
    # human will look.
    out = Path(args.out) if args.out else PROJECT_ROOT / "data" / "digest.txt"
    if not out.is_absolute():
        out = PROJECT_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(digest + "\n", encoding="utf-8")
    print(f"\nSaved to {out}")

    if report.failed_stages:
        print(f"{len(report.failed_stages)} stage(s) failed — see above. "
              f"Everything else still ran.")
    return report.exit_code()


def cmd_status(cfg, args) -> int:
    """Live progress of the long-running background jobs."""
    init_engine(cfg.database_url)
    import status as status_mod

    if args.watch:
        return status_mod.watch(cfg, interval=args.interval)

    tasks = status_mod.collect(cfg)
    print()
    print(status_mod.render(tasks))
    print()
    if any(t.running for t in tasks):
        print("  Add --watch for a live view that refreshes itself.")
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
    p_discover.add_argument(
        "--dry-run", action="store_true", help="Show the probes and their cost, send nothing"
    )
    p_discover.add_argument(
        "--max-slugs", type=int, default=0,
        help="Cap slug guesses per company (1 = domain/best guess only). "
             "The difference between a run that finishes and one that does not.",
    )

    p_names = sub.add_parser(
        "import-names", help="Filter a big company export into a discovery name list"
    )
    p_names.add_argument("--file", required=True, help="A .xlsx or .csv company export")
    p_names.add_argument("--sheet", default=None, help="Worksheet name (default: the first)")
    p_names.add_argument("--out", default="data/discovery_names.txt")
    p_names.add_argument("--countries", default="united states",
                         help="Comma-separated; empty string for all")
    p_names.add_argument("--min-employees", type=int, default=50)
    p_names.add_argument("--max-employees", type=int, default=5000)
    p_names.add_argument("--limit", type=int, default=0)

    p_mail = sub.add_parser(
        "scan-mail", help="Harvest company names from your mailbox (read-only)"
    )
    p_mail.add_argument("--mbox", help="Scan a .mbox export instead of connecting")
    p_mail.add_argument("--address", help="Overrides MAIL_ADDRESS from .env")
    p_mail.add_argument("--password", help="Overrides MAIL_APP_PASSWORD from .env")
    p_mail.add_argument("--host", default="imap.gmail.com")
    p_mail.add_argument("--folder", default="INBOX")
    p_mail.add_argument("--since", help='Only mail after this date, e.g. 01-Jan-2025')
    p_mail.add_argument("--limit", type=int, default=0, help="Cap messages scanned")
    p_mail.add_argument("--min-mentions", type=int, default=1,
                        help="Only keep names seen at least this many times")
    p_mail.add_argument("--out", default="data/mailbox_names.txt")
    p_mail.add_argument("--no-save-boards", action="store_true",
                        help="Do not add confirmed ATS boards to the company list")

    p_import = sub.add_parser("import-csv", help="Import an inventory CSV (name,slug,source)")
    p_import.add_argument("--file", required=True)

    p_export = sub.add_parser("export", help="Write discovered jobs to the Excel tracker")
    p_export.add_argument(
        "--all", action="store_true", help="Re-export every job, not just new ones"
    )

    sub.add_parser("reparse", help="Re-derive requirements from stored JDs (no network)")

    p_profile = sub.add_parser(
        "profile", help="Derive your job search from your resume (no keywords to write)"
    )
    p_profile.add_argument("--force", action="store_true", help="Regenerate, overwriting edits")
    p_profile.add_argument("--show", action="store_true", help="Print without saving")

    p_prune = sub.add_parser(
        "prune", help="Remove stored jobs that no longer match the current search"
    )
    p_prune.add_argument("--apply", action="store_true", help="Actually delete (default: preview)")

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
    p_tailor.add_argument(
        "--estimate", action="store_true", help="Show projected cost and exit without calling"
    )

    p_brief = sub.add_parser(
        "brief", help="Write a tailoring prompt to paste into any model (free, no API key)"
    )
    p_brief.add_argument("job_id", help="Job id, from `score`")

    p_accept = sub.add_parser(
        "accept", help="Read a model's reply back in, guard it, and write the resume"
    )
    p_accept.add_argument("job_id", help="Job id the reply is for")
    p_accept.add_argument("--file", required=True, help="File holding the model's JSON reply")

    p_daily = sub.add_parser(
        "daily", help="Scrape, retire, rank and export in one go, then print a digest"
    )
    p_daily.add_argument("--no-scrape", action="store_true",
                         help="Skip scraping; just re-rank and export what is stored")
    p_daily.add_argument("--since-hours", type=int, default=24,
                         help='What counts as "new" in the digest (default 24)')
    p_daily.add_argument("--top", type=int, default=10, help="New jobs to list in the digest")
    p_daily.add_argument("--out", default=None, help="Where to write the digest")

    p_status = sub.add_parser(
        "status", help="Progress of the long-running background jobs"
    )
    p_status.add_argument("--watch", action="store_true", help="Refresh until everything stops")
    p_status.add_argument("--interval", type=float, default=5.0, help="Seconds between refreshes")

    sub.add_parser("stats", help="Show DB counts and recent finds")

    p_failures = sub.add_parser("failures", help="Show logged scrape failures")
    p_failures.add_argument("--limit", type=int, default=30)

    return parser


COMMANDS = {
    "init-db": cmd_init_db,
    "scrape": cmd_scrape,
    "discover": cmd_discover,
    "scan-mail": cmd_scan_mail,
    "import-names": cmd_import_names,
    "import-csv": cmd_import_csv,
    "export": cmd_export,
    "reparse": cmd_reparse,
    "profile": cmd_profile,
    "prune": cmd_prune,
    "daily": cmd_daily,
    "status": cmd_status,
    "score": cmd_score,
    "tailor": cmd_tailor,
    "brief": cmd_brief,
    "accept": cmd_accept,
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
