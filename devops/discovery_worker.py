"""One discovery partition: its own boards read on its own machine, from its own copy of the saved database, and
handed back as a small result of its own (Nov 2026; plan: devops/discovery_plan.py, merge: devops/discovery_merge.py).

A partition reads exactly the boards the plan gave it -- all of one job site -- with that site's share of its budget
(boards read at once), under a time budget: after it no board is started and no rate-limit wait runs past it, and
those boards are recorded as not attempted or rate-limited, never as read. Closing a posting is still only ever done
for a board read successfully and completely (data_engineering/scraper/lifecycle.py); nothing here changes that.

It hands back, instead of the whole database:
  * part.db -- its boards' rows only (companies; postings of the boards it read; its scrape log), and a table of
    what happened to each board, the plan it belongs to and when it ran; and (Nov 2026) each board read completely,
    every posting id its listing named (`inventory`, devops/discovery_inventory.py). Locked before it leaves.
  * report.json -- counts only (boards per outcome, error kinds, minutes): safe for a public log.
Writing nothing else, it can be run again: a new attempt makes a new result for the same partition.

    python -m devops.discovery_worker --manifest manifest.json --partition greenhouse-1of2 --out out/
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

OUTCOMES = ("complete", "partial", "failed", "rate_limited", "not_attempted")
TABLES = ("companies", "jobs", "scrape_log")
QUOTA = "ProviderQuota"          # not asked this run: past its site's quota, its turn comes in a later run
COOLING = "ProviderCooling"      # not asked this run: its site is cooling off after refusing (devops/discovery_plan.py)
DETAIL = ("listing", "listed", "relevant", "described", "pending", "skipped")   # runner.RunSummary.details


def read_only(path) -> str:
    """A read-only SQLite address for a file, encoded (a space, '#' or '?' in a folder name is safe)."""
    return Path(path).resolve().as_uri() + "?mode=ro"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def find(manifest: dict, partition_id: str) -> dict:
    found = [p for p in manifest.get("partitions", []) if p.get("id") == partition_id]
    if len(found) != 1:
        raise ValueError(f"partition {partition_id!r} is not in the plan exactly once")
    part = found[0]
    sites = {b["source"] for b in part["boards"]}
    if sites != {part["provider"]}:
        raise ValueError(f"partition {partition_id} names boards of more than its own job site")
    return part


def ensure_boards(part: dict) -> dict[tuple, str]:
    """Each of the partition's boards in this machine's database: added when the saved copy does not have it yet
    (the plan read the newer board list). A board the copy holds as inactive is not read here."""
    from data_engineering.db.models import Company
    from data_engineering.db.session import get_session
    skipped: dict[tuple, str] = {}
    with get_session() as session:
        for b in part["boards"]:
            row = session.query(Company).filter_by(source=b["source"], slug=b["slug"]).one_or_none()
            if row is None:
                session.add(Company(name=b.get("name") or b["slug"], slug=b["slug"], source=b["source"],
                                    board_url=b.get("board_url") or None, origin=b.get("origin") or "imported",
                                    active=True))
            elif not row.active:
                skipped[(b["source"], b["slug"])] = "Inactive"
    return skipped


def rotation(refs: list, quota: int) -> tuple[list, list]:
    """(the boards to ask this run, the rest): at most `quota`, the least recently read first -- never read
    first -- so every board takes its turn and none waits for ever. Ties by slug, so the order is stable."""
    if not quota or len(refs) <= quota:
        return list(refs), []
    from data_engineering.db.models import Company
    from data_engineering.db.session import get_session
    with get_session() as session:
        last = {(c.source, c.slug): c.last_scraped_at for c in session.query(Company).filter(
            Company.source.in_({r.source for r in refs})).all()}
    order = sorted(refs, key=lambda r: (last.get((r.source, r.slug)) is not None,
                                        str(last.get((r.source, r.slug)) or ""), r.slug))
    return order[:quota], order[quota:]


def run(cfg, manifest: dict, partition_id: str, budget_minutes: float | None = None,
        run_id: str | None = None) -> dict:
    """Read the partition's boards; return what happened, board by board, and the run's id."""
    from data_engineering.scraper.runner import NOT_ATTEMPTED, SCRAPER_TYPES, scrape_companies
    from data_engineering.scraper.base import CompanyRef
    from data_engineering.scraper.http_client import HttpSettings, PoliteClient
    part = find(manifest, partition_id)
    started_at, started = _now(), time.monotonic()
    budget = float(budget_minutes if budget_minutes is not None else manifest.get("budget_minutes", 150))
    outcomes: dict[tuple, tuple] = {}
    details: dict[tuple, dict] = {}
    inventory: dict[tuple, dict] = {}
    retry: dict = {}
    client = None
    if part["provider"] not in SCRAPER_TYPES or not cfg.get(f"portals.{part['provider']}.enabled", True):
        for b in part["boards"]:
            outcomes[(b["source"], b["slug"])] = (NOT_ATTEMPTED, "NoReader")
        summary_id = run_id or f"{partition_id}-none"
    elif part.get("cooling_until"):
        # The site refused every board of its last probes, or asked us to stay away (Retry-After): until the time
        # the plan names, not one of its boards is asked -- each kept for the runs after (27 Nov 2026).
        for b in part["boards"]:
            outcomes[(b["source"], b["slug"])] = (NOT_ATTEMPTED, COOLING)
        summary_id = run_id or f"{partition_id}-cooling"
    else:
        skipped = ensure_boards(part)
        refs = [CompanyRef(name=b.get("name") or b["slug"], slug=b["slug"], source=b["source"])
                for b in part["boards"] if (b["source"], b["slug"]) not in skipped]
        refs, later = rotation(refs, int(part.get("quota") or 0))
        settings = HttpSettings.from_config(cfg)
        if part.get("min_interval_s"):
            # This reader's share of its site's request-rate cap, across every reader of the site on every runner.
            settings.min_interval_seconds = max(settings.min_interval_seconds, float(part["min_interval_s"]))
        client = PoliteClient(settings)
        client.preload_robots(known_robots(database_file()))
        if part.get("min_interval_s") and int(part.get("of") or 1) > 1:
            # Readers of one site start together; each waits its place in the site's interval first, so their first
            # requests are spread over it rather than sent in one burst.
            time.sleep(float(part["min_interval_s"]) * (int(part.get("index") or 1) - 1) / int(part["of"]))
        summary = scrape_companies(cfg, refs, workers=int(part.get("workers") or 1),
                                   deadline=started + budget * 60, client=client, run_id=run_id,
                                   retry_in_run=bool(part.get("in_run_retry", True)))
        retry = dict(summary.retry)
        summary_id = summary.run_id
        outcomes.update(summary.outcomes)
        details.update(summary.details)
        inventory.update(getattr(summary, "inventory", {}) or {})
        for ref in later:
            outcomes[(ref.source, ref.slug)] = (NOT_ATTEMPTED, QUOTA)
        for key, why in skipped.items():
            outcomes[key] = (NOT_ATTEMPTED, why)
    for b in part["boards"]:                       # a board with no outcome was never started
        outcomes.setdefault((b["source"], b["slug"]), (NOT_ATTEMPTED, "NotStarted"))
    events = dict(getattr(client, "rate_limit_events", {}) or {})
    # Each answer stamped with when it was FETCHED (not when the run ended): 24 hours at most from that moment.
    robots = {h: [v[0], v[1], v[2] if len(v) > 2 else _now()] for h, v in (getattr(client, "robots_seen", {}) or {}).items()}
    return {"partition": partition_id, "provider": part["provider"], "plan_id": manifest["plan_id"],
            "run_id": summary_id, "started_at": started_at, "finished_at": _now(),
            "minutes": round((time.monotonic() - started) / 60, 2), "budget_minutes": budget,
            "outcomes": {f"{s}\t{slug}": list(v) for (s, slug), v in outcomes.items()},
            "details": {f"{s}\t{slug}": v for (s, slug), v in details.items()},
            "inventory": {f"{s}\t{slug}": v for (s, slug), v in inventory.items()
                          if outcomes.get((s, slug), (None,))[0] == "complete"},
            "rate_limited_requests": sum(v for k, v in events.items() if not k.startswith("provider:")),
            "requests": int(getattr(client, "requests_made", 0) or 0), "retry": retry,
            "min_interval_s": float(part.get("min_interval_s") or 0), "robots": robots,
            "longest_retry_after_s": round(float(getattr(client, "longest_retry_after", 0) or 0), 1),
            "provider_rate_limits": {k.split(":", 1)[1]: v for k, v in events.items() if k.startswith("provider:")}}


def export(db_path: Path, result: dict, manifest: dict, out: Path) -> Path:
    """part.db: the partition's boards' rows only, and what happened to each, from this machine's database."""
    part = find(manifest, result["partition"])
    out.mkdir(parents=True, exist_ok=True)
    target = out / "part.db"
    if target.exists():
        target.unlink()
    read = {k for k, v in result["outcomes"].items() if v[0] in ("complete", "partial")}
    src = sqlite3.connect(read_only(db_path), uri=True)
    dst = sqlite3.connect(target)
    try:
        for table in TABLES:
            sql = src.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
            if sql is None:
                raise RuntimeError(f"the database has no {table} table")
            dst.execute(sql[0])
        dst.execute("CREATE TABLE boards (source TEXT, slug TEXT, status TEXT, error_type TEXT, listing TEXT, "
                    "listed INTEGER, relevant INTEGER, described INTEGER, pending INTEGER, skipped INTEGER, "
                    "PRIMARY KEY (source, slug))")
        dst.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        cols = {t: [r[1] for r in src.execute(f"PRAGMA table_info({t})")] for t in TABLES}
        mine = [(b["source"], b["slug"]) for b in part["boards"]]
        for source, slug in mine:
            row = src.execute(f"SELECT {', '.join(cols['companies'])} FROM companies WHERE source = ? AND slug = ?",
                              (source, slug)).fetchone()
            if row is not None:
                dst.execute(f"INSERT INTO companies ({', '.join(cols['companies'])}) VALUES "
                            f"({', '.join('?' for _ in cols['companies'])})", row)
            if f"{source}\t{slug}" in read:
                for job in src.execute(f"SELECT {', '.join(cols['jobs'])} FROM jobs WHERE source = ? AND "
                                       f"company_slug = ?", (source, slug)):
                    dst.execute(f"INSERT INTO jobs ({', '.join(cols['jobs'])}) VALUES "
                                f"({', '.join('?' for _ in cols['jobs'])})", job)
            status, error = result["outcomes"][f"{source}\t{slug}"]
            d = (result.get("details") or {}).get(f"{source}\t{slug}") or {}
            dst.execute("INSERT INTO boards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (source, slug, status, error or "", *(d.get(k) for k in DETAIL)))
        for row in src.execute(f"SELECT {', '.join(cols['scrape_log'])} FROM scrape_log WHERE run_id = ?",
                               (result["run_id"],)):
            dst.execute(f"INSERT INTO scrape_log ({', '.join(cols['scrape_log'])}) VALUES "
                        f"({', '.join('?' for _ in cols['scrape_log'])})", row)
        # Each board read completely: every id its listing named (devops/discovery_inventory.py).
        from devops.discovery_inventory import write_part
        write_part(dst, result.get("inventory") or {}, result["outcomes"], result["run_id"])
        # robots.txt answers read here, for the next runs' readers (24 hours at most): a table, not meta.
        dst.execute(ROBOTS_SCHEMA)
        dst.executemany("INSERT OR REPLACE INTO robots_cache VALUES (?, ?, ?, ?)",
                        [(h, int(st), body or "", at) for h, (st, body, at) in (result.get("robots") or {}).items()])
        meta = {k: v for k, v in result.items() if k not in ("outcomes", "details", "inventory", "robots")}
        for key, value in meta.items():
            dst.execute("INSERT INTO meta VALUES (?, ?)", (key, json.dumps(value)))
        dst.commit()
    finally:
        src.close()
        dst.close()
    return target


ROBOTS_SCHEMA = ("CREATE TABLE IF NOT EXISTS robots_cache (host_root TEXT PRIMARY KEY, status INTEGER NOT NULL, "
                 "body TEXT NOT NULL, fetched_at TEXT NOT NULL)")
# RFC 9309 §2.4: a crawler SHOULD NOT use a cached robots.txt for more than 24 hours. 23 here, so an answer taken at
# a run's start is still under 24 hours old at its end.
ROBOTS_MAX_AGE_H = 23


def known_robots(db_path: Path, now: datetime | None = None) -> dict:
    """robots.txt answers read in the last 23 hours (the saved database's robots_cache), {host_root: (status, text)}:
    definite answers only (http_client.ROBOTS_KEEP -- never a 429, a 5xx or anything else). None of them, or no such
    table (an older database): {} -- every robots.txt is asked for, as before."""
    from data_engineering.scraper.http_client import ROBOTS_KEEP
    from datetime import timedelta
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(hours=ROBOTS_MAX_AGE_H)).isoformat(timespec="seconds")
    try:
        con = sqlite3.connect(read_only(db_path), uri=True)
    except sqlite3.Error:
        return {}
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'robots_cache'").fetchone():
            return {}
        return {h: (st, body) for h, st, body in
                con.execute("SELECT host_root, status, body FROM robots_cache WHERE fetched_at >= ?", (cutoff,))
                if isinstance(st, int) and st in ROBOTS_KEEP}
    finally:
        con.close()


def database_file() -> Path:
    """The SQLite file the app opened (data_engineering.db.session resolves a relative path against the project)."""
    from data_engineering.db import session as session_mod
    found = getattr(getattr(session_mod, "_engine", None), "url", None)
    if found is None or not found.database or found.database == ":memory:":
        raise RuntimeError("the partition needs its own database file")
    return Path(found.database)


def report(result: dict) -> dict:
    """Counts only -- never a board -- for the public log."""
    counts = {o: 0 for o in OUTCOMES}
    errors: dict[str, int] = {}
    for status, error in result["outcomes"].values():
        counts[status] = counts.get(status, 0) + 1
        if status in ("failed", "rate_limited", "not_attempted") and error:
            errors[error] = errors.get(error, 0) + 1
    postings = {k: sum(int(d.get(k) or 0) for d in (result.get("details") or {}).values())
                for k in ("listed", "relevant", "described", "pending", "skipped")}
    listing = {"complete": 0, "incomplete": 0}
    for d in (result.get("details") or {}).values():
        listing[d.get("listing", "complete")] = listing.get(d.get("listing", "complete"), 0) + 1
    return {"partition": result["partition"], "provider": result["provider"], "plan_id": result["plan_id"],
            "boards": len(result["outcomes"]), "counts": counts, "errors": errors,
            "postings": postings, "listings": listing,
            "started_at": result["started_at"], "finished_at": result["finished_at"], "minutes": result["minutes"],
            "budget_minutes": result["budget_minutes"], "rate_limited_requests": result["rate_limited_requests"],
            "provider_rate_limits": result["provider_rate_limits"], "requests": result.get("requests", 0),
            "retry": result.get("retry") or {}, "delivered": True}


def failed_report(manifest: dict | None, partition_id: str, why: str) -> dict:
    """What a partition that could not finish says about itself: its boards were not read here."""
    boards = 0
    provider = ""
    if manifest:
        try:
            part = find(manifest, partition_id)
            boards, provider = len(part["boards"]), part["provider"]
        except ValueError:
            pass
    return {"partition": partition_id, "provider": provider, "plan_id": (manifest or {}).get("plan_id", ""),
            "boards": boards, "counts": {o: 0 for o in OUTCOMES} | {"not_attempted": boards},
            "errors": {why[:60]: boards}, "delivered": False, "finished_at": _now()}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--partition", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--budget-minutes", default="")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    # The scraper's own log lines name boards ("FAILED source/slug ...") and the searched titles, and this job's
    # log is public on a public repository: they go to a file on this machine only (25 Nov 2026; before, the
    # default handler printed every warning to the public log). The worker's own lines are counts only.
    import logging
    logging.basicConfig(filename=str(args.out.parent / "worker-private.log"), level=logging.INFO, force=True)
    manifest = None
    try:
        manifest = json.loads(args.manifest.read_text())
        from backend.config.loader import load_config
        from data_engineering.db.session import init_engine
        cfg = load_config()
        init_engine(cfg.database_url)
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
        run_id = "-".join(x for x in (os.environ.get("GITHUB_RUN_ID", ""), attempt, args.partition) if x) or None
        budget = float(args.budget_minutes) if str(args.budget_minutes).strip() else None
        result = run(cfg, manifest, args.partition, budget, run_id)
        export(database_file(), result, manifest, args.out)
        said = report(result)
    except Exception as exc:                          # noqa: BLE001 -- said in the report, and the job fails
        said = failed_report(manifest, args.partition, f"{type(exc).__name__}")
        (args.out / "report.json").write_text(json.dumps(said, indent=1))
        print(f"partition {args.partition}: could not finish ({type(exc).__name__})", file=sys.stderr)
        return 1
    (args.out / "report.json").write_text(json.dumps(said, indent=1))
    c = said["counts"]
    print(f"partition {args.partition}: {said['boards']:,} boards -- {c['complete']:,} complete, {c['partial']:,} "
          f"partial, {c['failed']:,} failed, {c['rate_limited']:,} rate-limited, {c['not_attempted']:,} not attempted "
          f"({said['minutes']} min)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
