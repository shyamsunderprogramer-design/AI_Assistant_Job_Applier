"""The job-search sites and job-alert emails, read in the cloud beside the board partitions (Nov 2026).

Adzuna, Workable's official job feed (data_engineering/scraper/workable_feed.py), the job feeds (USAJobs, Jooble, Careerjet, Findwork, The Muse, Remotive, ...) and, when a dedicated
job-alert mailbox is configured, the alert emails: each read on its own copy of the saved database, exactly as
the daily run reads them, with the same filter, the same duplicate check against the boards' postings and the
same 21-day retirement rule for a feed posting no longer seen. Keys come from the workflow's encrypted secrets,
and a source whose key is not set is reported as skipped, never as read.

The result is only what this job changed: out/feeds.db holds the jobs rows of these sources that were added,
seen again or retired since it started (the join merges them into the saved database, discovery_merge
merge_feeds), and out/feeds-report.json the counts per source and which sources were read. Counts only in the
log: no posting, no key, no address, no searched title (the sources' own log lines go to a file on the machine).

    python -m devops.discovery_feeds --out out
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _alerts_configured() -> bool:
    return bool((os.getenv("MAIL_ADDRESS") or "").strip() and (os.getenv("MAIL_APP_PASSWORD") or "").strip())


def run(cfg, out: Path) -> dict:
    """Read every source that can be read here; returns the report (also written to out/feeds-report.json)."""
    from data_engineering.db.models import utcnow

    out.mkdir(parents=True, exist_ok=True)
    started = utcnow().replace(tzinfo=None)
    clock = time.monotonic()
    report: dict = {"started_at": started.isoformat(timespec="seconds"), "sources": {}, "read": []}

    def note(name: str, **counts) -> None:
        report["sources"][name] = counts
        if counts.get("status") == "read":
            report["read"].append(name)

    # -- Adzuna --------------------------------------------------------------
    from data_engineering.scraper.adzuna import SOURCE as ADZUNA, credentials
    if credentials():
        from data_engineering.scraper.adzuna import run as run_adzuna
        try:
            s = run_adzuna(cfg)
            note(ADZUNA, status="skipped" if s.skipped else "read", calls=s.calls, matched=s.matched, new=s.new,
                 seen=s.seen, duplicates=s.duplicates, retired=s.retired, why=s.skipped or "")
        except Exception as exc:                  # noqa: BLE001 -- one source never stops the others
            note(ADZUNA, status="failed", why=f"{type(exc).__name__}: {str(exc)[:120]}")
    else:
        note(ADZUNA, status="skipped", why="ADZUNA_APP_ID and ADZUNA_APP_KEY are not set")

    # -- Workable's official job feed (one file a run; never in place of the Workable boards) -------------------
    from data_engineering.scraper import workable_feed
    try:
        w = workable_feed.run(cfg, workdir=out.parent)
        note(workable_feed.SOURCE, status=w.status, calls=w.calls, jobs=w.jobs, matched=w.matched, new=w.new,
             seen=w.seen, on_boards=w.on_boards, duplicates=w.duplicates, retired=w.retired,
             complete=w.complete, megabytes=round(w.bytes / 1e6, 1), seconds=w.seconds, why=w.why)
    except Exception as exc:                      # noqa: BLE001
        note(workable_feed.SOURCE, status="failed", why=f"{type(exc).__name__}: {str(exc)[:120]}")

    # -- the job feeds ---------------------------------------------------------
    from data_engineering.scraper.feeds import run_feeds
    try:
        for s in run_feeds(cfg):
            status = "skipped" if s.skipped else ("failed" if s.error else "read")
            note(s.name, status=status, calls=s.calls, matched=s.matched, new=s.new, seen=s.seen,
                 duplicates=s.duplicates, retired=s.retired, why=s.skipped or s.error)
    except Exception as exc:                      # noqa: BLE001
        note("feeds", status="failed", why=f"{type(exc).__name__}: {str(exc)[:120]}")

    # -- job-alert emails: a dedicated mailbox only, opened read-only ------------
    if _alerts_configured():
        try:
            from data_engineering.scraper.alert_import import fetch_alerts, store
            new, seen = store(fetch_alerts(since_days=int(cfg.get("alerts.since_days", 3))))[:2]
            note("alerts", status="read", new=new, seen=seen)
        except Exception as exc:                  # noqa: BLE001
            note("alerts", status="failed", why=f"{type(exc).__name__}: {str(exc)[:120]}")
    else:
        note("alerts", status="skipped", why="no dedicated job-alert mailbox is configured")

    report["rows"], report["row_sources"] = export(Path(cfg.database_url.replace("sqlite:///", "", 1)),
                                                   out / "feeds.db", started)
    report["minutes"] = round((time.monotonic() - clock) / 60, 1)
    report["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (out / "feeds-report.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    return report


def export(db: Path, dest: Path, since: datetime) -> tuple[int, list[str]]:
    """The rows of sources that are not boards (no `companies` row) added, seen again or closed since `since`,
    into a fresh `dest` with the same jobs table. Returns (rows, their sources)."""
    if dest.exists():
        dest.unlink()
    stamp = since.strftime("%Y-%m-%d %H:%M:%S")
    src = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        schema = src.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()[0]
        boards = {r[0] for r in src.execute("SELECT DISTINCT source FROM companies")}
        cols = [r[1] for r in src.execute("PRAGMA table_info(jobs)")]
        rows = [r for r in src.execute(
            f"SELECT {', '.join(cols)} FROM jobs WHERE found_at >= ? OR last_seen_at >= ? OR closed_at >= ?",
            (stamp, stamp, stamp)) if r[cols.index("source")] not in boards]
    finally:
        src.close()
    out = sqlite3.connect(dest)
    try:
        out.execute(schema)
        out.executemany(f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", rows)
        out.commit()
    finally:
        out.close()
    return len(rows), sorted({r[cols.index("source")] for r in rows})


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    # The sources' own log lines name the searched titles and their request addresses, and this job's log is
    # public on a public repository: they go to a file on this machine only, never to the job's output.
    import logging
    logging.basicConfig(filename=str(args.out.parent / "feeds-private.log"), level=logging.INFO, force=True)
    try:
        from backend.config.loader import load_config
        from data_engineering.db.session import init_engine
        cfg = load_config()
        init_engine(cfg.database_url)
        report = run(cfg, args.out)
    except Exception as exc:                      # noqa: BLE001 -- its type only: the message may name a search
        print(f"the job-search sites were not read ({type(exc).__name__})", file=sys.stderr)
        return 1
    for name, c in sorted(report["sources"].items()):
        extra = "" if c.get("status") != "read" else f": {c.get('new', 0)} new, {c.get('seen', 0)} seen again"
        print(f"{name}: {c.get('status')}{extra}")
    print(f"{report['rows']} rows from {len(report['row_sources'])} sources in {report['minutes']} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
