"""Discovery partitions merged into the saved database, each on its own, and a coverage report (Nov 2026).

Every partition's result (devops/discovery_worker.py) is checked before anything of it is used: it is a readable
SQLite file that passes its integrity check, it belongs to THIS plan (plan id) and to this partition, and every row in
it is one of that partition's own boards on its own job site. A result that fails any of that is not used -- that
partition is FAILED -- and the others are merged all the same: one site's trouble never spoils another's work.

What is taken, board by board:
  * every board's own row (its last read, its failure count), never an older one over a newer;
  * postings only from boards read (complete or partly), never from a board that failed, was rate-limited or was not
    reached -- so a posting is closed only where its board was really read (the worker's lifecycle rule), and a
    newer posting row is never overwritten by an older one;
  * the partition's scrape log rows, once (a result delivered twice adds nothing the second time).
Every posting of a board taken must be in the result exactly once.

The merge works on a copy beside the database and replaces it in one step only when the copy passes its integrity
check: interrupted at any point, the database is as it was (a left-over copy is removed next time).

Coverage, per partition, site and run: COMPLETE (every board read in full), PARTIAL (its result arrived, but some
boards were read only in part, failed, were rate-limited or not reached in time), FAILED (planned, and its result is
missing, unreadable or not this plan's), NOT ATTEMPTED (the plan left it out, or not one of its boards was started).
The board counts say which.

A result arriving is not a refresh: `complete_refresh` -- what lets the run end in success and publish the older
complete-run artifact -- is true only when the run is COMPLETE, every board of every partition read in full. Anything
less is still merged into the saved database (closing nothing that was not read), the run fails, and the verified
result (devops/discovery_result.py) says board by board what was read: the Mac takes those boards and reads only the
others itself (devops/cloud_results.py, devops/cloud_recovery.py). `outcomes` names every board, so it is kept out
of the public coverage report (`public`) and travels only inside the locked result.

    python -m devops.discovery_merge --db data/jobs.db --manifest manifest.json --parts-dir parts \\
        --reports-dir reports --coverage coverage.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

COMPLETE, PARTIAL, FAILED, NOT_ATTEMPTED = "COMPLETE", "PARTIAL", "FAILED", "NOT ATTEMPTED"
READ = ("complete", "partial")
BOARD_STATES = ("complete", "partial", "failed", "rate_limited", "not_attempted")
KEYS = {"companies": ("source", "slug"), "jobs": ("source", "company_slug", "external_id")}


class MergeError(RuntimeError):
    pass


def read_only(path) -> str:
    """A read-only SQLite address for a file, encoded (a space, '#' or '?' in a folder name is safe)."""
    return Path(path).resolve().as_uri() + "?mode=ro"


def _cols(db: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in db.execute(f"PRAGMA {schema}.table_info({table})")]


def _side_files(base: Path) -> list[Path]:
    return [p for p in (base.with_name(base.name + "-wal"), base.with_name(base.name + "-journal")) if p.exists()]


def settle(base: Path) -> None:
    """Make the saved database ONE file before it is copied or replaced. The app keeps it in WAL mode
    (data_engineering/db/session.py), where recent writes live in a jobs.db-wal beside it: copying only
    jobs.db would miss them, and a -wal left beside a replaced file is replayed onto it and corrupts it.
    So its WAL is folded in and it is switched to a rollback journal -- its contents unchanged. Refuses,
    changing nothing, if another connection holds it open."""
    if not base.exists():
        return
    con = sqlite3.connect(base, timeout=10)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    except sqlite3.Error as exc:
        raise MergeError(f"the saved database could not be settled ({type(exc).__name__}); nothing was changed") from exc
    finally:
        con.close()
    if str(mode).lower() != "delete" or _side_files(base):
        raise MergeError("the saved database is open elsewhere; nothing was changed")


def check_part(path: Path | None, manifest: dict, part: dict) -> tuple[str | None, dict]:
    """(why the result cannot be used, or None; what it says about each board)."""
    if path is None or not Path(path).is_file():
        return "its result did not arrive", {}
    try:
        db = sqlite3.connect(read_only(path), uri=True)
    except sqlite3.Error as exc:
        return f"its result cannot be opened ({type(exc).__name__})", {}
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            return "its result failed its integrity check", {}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"companies", "jobs", "scrape_log", "boards", "meta"} <= tables:
            return "its result is not a partition result", {}
        meta = {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta")}
        if meta.get("plan_id") != manifest["plan_id"]:
            return "its result belongs to another plan", {}
        if meta.get("partition") != part["id"]:
            return "its result belongs to another partition", {}
        own = {(b["source"], b["slug"]) for b in part["boards"]}
        boards = {(s, slug): (status, err) for s, slug, status, err in
                  db.execute("SELECT source, slug, status, error_type FROM boards")}
        if set(boards) != own:
            return "its result does not account for exactly its own boards", {}
        if any(status not in BOARD_STATES for status, _ in boards.values()):
            return "its result names an unknown board outcome", {}
        for table, col in (("companies", "slug"), ("jobs", "company_slug"), ("scrape_log", "company_slug")):
            stray = [r for r in db.execute(f"SELECT source, {col} FROM {table}") if (r[0], r[1]) not in own]
            if stray:
                return f"its result holds {table} rows of boards that are not its own", {}
        unread = [r for r in db.execute("SELECT source, company_slug FROM jobs")
                  if boards[(r[0], r[1])][0] not in READ]
        if unread:
            return "its result holds postings of boards it did not read", {}
        return None, {"boards": boards, "meta": meta}
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        return f"its result cannot be read ({type(exc).__name__})", {}
    finally:
        db.close()


def _merge_one(db: sqlite3.Connection, path: Path, boards: dict) -> dict:
    """One checked result into the open copy, in one transaction. Returns what it did."""
    done = {"added": 0, "updated": 0, "older_skipped": 0, "logged": 0}
    db.execute("ATTACH DATABASE ? AS part", (read_only(path),))
    try:
        db.execute("BEGIN IMMEDIATE")
        read = [b for b, (status, _) in boards.items() if status in READ]
        db.execute("CREATE TEMP TABLE IF NOT EXISTS read_boards (source TEXT, slug TEXT)")
        db.execute("DELETE FROM read_boards")
        db.executemany("INSERT INTO read_boards VALUES (?, ?)", read)
        # Boards: never an older row over a newer one (last read time).
        shared = [c for c in _cols(db, "main", "companies") if c != "id" and c in _cols(db, "part", "companies")]
        cols = ", ".join(shared)
        sets = ", ".join(f"{c} = excluded.{c}" for c in shared if c not in KEYS["companies"])
        db.execute(f"INSERT INTO main.companies ({cols}) SELECT {cols} FROM part.companies WHERE true "
                   f"ON CONFLICT(source, slug) DO UPDATE SET {sets} "
                   f"WHERE coalesce(excluded.last_scraped_at, '') >= coalesce(companies.last_scraped_at, '')")
        # Postings of the boards read only; never an older row over a newer one (last seen).
        shared = [c for c in _cols(db, "main", "jobs") if c != "id" and c in _cols(db, "part", "jobs")]
        cols = ", ".join(shared)
        sets = ", ".join(f"{c} = excluded.{c}" for c in shared if c not in KEYS["jobs"])
        before = db.execute("SELECT COUNT(*) FROM main.jobs").fetchone()[0]
        older = db.execute(
            "SELECT COUNT(*) FROM part.jobs p JOIN main.jobs m ON m.source = p.source AND "
            "m.company_slug = p.company_slug AND m.external_id = p.external_id "
            "WHERE p.last_seen_at IS NOT NULL AND m.last_seen_at IS NOT NULL AND p.last_seen_at < m.last_seen_at"
        ).fetchone()[0]
        incoming = db.execute("SELECT COUNT(*) FROM part.jobs").fetchone()[0]
        db.execute(f"INSERT INTO main.jobs ({cols}) SELECT {cols} FROM part.jobs p WHERE EXISTS "
                   f"(SELECT 1 FROM read_boards r WHERE r.source = p.source AND r.slug = p.company_slug) "
                   f"ON CONFLICT(source, company_slug, external_id) DO UPDATE SET {sets} "
                   f"WHERE excluded.last_seen_at IS NULL OR jobs.last_seen_at IS NULL "
                   f"OR excluded.last_seen_at >= jobs.last_seen_at")
        after = db.execute("SELECT COUNT(*) FROM main.jobs").fetchone()[0]
        done["added"] = after - before
        done["older_skipped"] = older
        done["updated"] = incoming - done["added"] - older
        lost = db.execute("SELECT COUNT(*) FROM part.jobs p WHERE (SELECT COUNT(*) FROM main.jobs m WHERE "
                          "m.source = p.source AND m.company_slug = p.company_slug AND m.external_id = p.external_id)"
                          " != 1").fetchone()[0]
        if lost:
            raise MergeError(f"{lost} posting(s) not in the result exactly once")
        # Its scrape log, once.
        shared = [c for c in _cols(db, "main", "scrape_log") if c != "id" and c in _cols(db, "part", "scrape_log")]
        cols = ", ".join(shared)
        cur = db.execute(f"INSERT INTO main.scrape_log ({cols}) SELECT {cols} FROM part.scrape_log p WHERE NOT EXISTS "
                         f"(SELECT 1 FROM main.scrape_log m WHERE m.run_id = p.run_id AND m.source = p.source "
                         f"AND m.company_slug = p.company_slug)")
        done["logged"] = cur.rowcount
        db.execute("COMMIT")
        return done
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.execute("DETACH DATABASE part")


def _status(counts: dict, delivered: bool, ran: bool) -> str:
    """A partition's coverage from what happened to its boards -- never from its result merely arriving."""
    if not delivered:
        return FAILED if ran else NOT_ATTEMPTED
    total = sum(counts.values())
    if total and counts.get("not_attempted", 0) == total:
        return NOT_ATTEMPTED                      # it ran, and its time was up before a single board started
    return COMPLETE if total and counts.get("complete", 0) == total else PARTIAL


def _roll_up(statuses: list[str]) -> str:
    if statuses and all(s == COMPLETE for s in statuses):
        return COMPLETE
    if statuses and all(s == NOT_ATTEMPTED for s in statuses):
        return NOT_ATTEMPTED
    if statuses and all(s in (FAILED, NOT_ATTEMPTED) for s in statuses):
        return FAILED
    return PARTIAL


def merge(base: Path, manifest: dict, parts: dict[str, Path | None], reports: dict[str, dict] | None = None,
          now: datetime | None = None) -> dict:
    """Every usable result merged into `base` (replaced in one step); the coverage report. A partition whose
    result cannot be used, or whose merge fails, is FAILED and the rest go on. Raises MergeError, leaving `base`
    as it was, only when the merged copy itself cannot be made sound."""
    reports = reports or {}
    base = Path(base)
    work = base.with_name(base.name + ".merging")
    if work.exists():
        work.unlink()                                   # an interrupted earlier merge: its copy is not used
    for left in _side_files(work):
        left.unlink()
    settle(base)
    if base.exists():
        shutil.copy2(base, work)
    elif any(parts.values()):
        # No saved database at all (a cold start): the first readable result's tables, empty, are the start.
        first = next((p for p in parts.values() if p and Path(p).is_file()), None)
        if first is None:
            raise MergeError("no saved database and no readable result")
        with sqlite3.connect(read_only(first), uri=True) as src, sqlite3.connect(work) as dst:
            for (sql,) in src.execute("SELECT sql FROM sqlite_master WHERE type = 'table' "
                                      "AND name IN ('companies', 'jobs', 'scrape_log')"):
                dst.execute(sql)
    else:
        raise MergeError("no saved database and no results")
    coverage = {"plan_id": manifest["plan_id"], "at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                "partitions": [], "providers": {}, "boards": {s: 0 for s in BOARD_STATES}, "outcomes": []}
    # uri=True: ATTACH then reads each result through a read-only address -- it is never written.
    db = sqlite3.connect(work.resolve().as_uri(), uri=True, isolation_level=None)
    try:
        for part in manifest["partitions"]:
            pid = part["id"]
            # Planned to run and nothing usable came back: FAILED. NOT ATTEMPTED is only a partition the plan
            # itself left out (--only) -- never a machine that went missing.
            ran = bool(part.get("run", True))
            why, found = check_part(parts.get(pid), manifest, part) if part.get("run", True) else ("not run", {})
            did = {}
            if why is None:
                try:
                    did = _merge_one(db, Path(parts[pid]), found["boards"])
                except (sqlite3.Error, MergeError) as exc:
                    why = f"its merge failed ({type(exc).__name__}): nothing of it was kept"
            counts = {s: 0 for s in BOARD_STATES}
            if why is None:
                for status, _ in found["boards"].values():
                    counts[status] += 1
            else:
                counts["not_attempted"] = len(part["boards"])
            status = _status(counts, why is None, ran)
            entry = {"id": pid, "provider": part["provider"], "boards": len(part["boards"]), "status": status,
                     "counts": counts, "merged": did, "reason": why or ""}
            meta = found.get("meta") or {}
            for key in ("started_at", "finished_at", "minutes", "rate_limited_requests", "provider_rate_limits"):
                if key in meta:
                    entry[key] = meta[key]
            coverage["partitions"].append(entry)
            for s, n in counts.items():
                coverage["boards"][s] += n
            # Board by board, for the verified result the Mac reuses (devops/discovery_result.py). Private: it
            # names every board, so it travels only inside the locked result, never in the public report.
            for b in part["boards"]:
                key = (b["source"], b["slug"])
                if why is None:
                    st, err = found["boards"][key]
                    if st == "not_attempted" and err == "TimeBudget":
                        st = "timed_out"                  # started too late in its partition's time, not skipped
                else:
                    st, err = "not_attempted", ("PartitionFailed" if ran else "NotRun")
                coverage["outcomes"].append([key[0], key[1], st, err or "", pid])
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MergeError("the merged copy failed its integrity check; the database was left as it was")
    except BaseException:
        db.close()
        work.unlink(missing_ok=True)
        raise
    db.close()
    if _side_files(work) or _side_files(base):
        work.unlink(missing_ok=True)
        raise MergeError("the saved database was opened while merging; it was left as it was")
    os.replace(work, base)                              # the one step: before it, the database is untouched
    by_site: dict[str, list] = {}
    for entry in coverage["partitions"]:
        by_site.setdefault(entry["provider"], []).append(entry)
    for site, entries in sorted(by_site.items()):
        counts = {s: sum(e["counts"][s] for e in entries) for s in BOARD_STATES}
        coverage["providers"][site] = {"status": _roll_up([e["status"] for e in entries]),
                                       "partitions": len(entries), "boards": sum(e["boards"] for e in entries),
                                       "counts": counts}
    statuses = [e["status"] for e in coverage["partitions"]]
    coverage["status"] = _roll_up(statuses)
    # Every result arriving is reported, but it is not a refresh: only every board read in full is.
    coverage["all_arrived"] = bool(statuses) and all(e["reason"] == "" for e in coverage["partitions"])
    coverage["complete_refresh"] = coverage["status"] == COMPLETE
    coverage["totals"] = totals(coverage)
    return coverage


def totals(coverage: dict, started: datetime | None = None, finished: datetime | None = None) -> dict:
    """The run in numbers -- counts only, safe for the public summary."""
    by = {}
    for _, _, st, _, _ in coverage["outcomes"]:
        by[st] = by.get(st, 0) + 1
    planned = len(coverage["outcomes"])
    scanned = by.get("complete", 0) + by.get("partial", 0)
    statuses = [e["status"] for e in coverage["partitions"]]
    out = {"partitions_complete": statuses.count(COMPLETE), "partitions_partial": statuses.count(PARTIAL),
           "partitions_failed": statuses.count(FAILED), "partitions_not_attempted": statuses.count(NOT_ATTEMPTED),
           "boards_planned": planned, "boards_scanned_in_full": by.get("complete", 0),
           "boards_read_in_part": by.get("partial", 0),
           "boards_needing_recovery": planned - scanned,
           "boards_deferred_provider_limits": by.get("rate_limited", 0),
           "boards_failed": by.get("failed", 0), "boards_timed_out": by.get("timed_out", 0),
           "boards_not_attempted": by.get("not_attempted", 0),
           "coverage_pct": round(100.0 * scanned / planned, 1) if planned else 0.0}
    if started and finished:
        out["duration_minutes"] = round((finished - started).total_seconds() / 60, 1)
    return out


def public(coverage: dict) -> dict:
    """The coverage report as published: everything but the board-by-board outcomes."""
    return {k: v for k, v in coverage.items() if k != "outcomes"}


def summary(coverage: dict) -> str:
    """Markdown for the run's public summary: sites and counts only."""
    lines = [f"### Discovery coverage: {coverage['status']}"
             + ("" if coverage["complete_refresh"] else
                " (not a complete refresh: kept as partial, and the Mac reads the boards itself)"),
             "", "| Site | Status | Partitions | Boards | Complete | Partial | Failed | Rate-limited | Not attempted |",
             "|---|---|---|---|---|---|---|---|---|"]
    for site, p in coverage["providers"].items():
        c = p["counts"]
        lines.append(f"| {site} | {p['status']} | {p['partitions']} | {p['boards']:,} | {c['complete']:,} | "
                     f"{c['partial']:,} | {c['failed']:,} | {c['rate_limited']:,} | {c['not_attempted']:,} |")
    t = coverage.get("totals") or {}
    if t:
        lines += ["", f"Partitions: {t['partitions_complete']} complete, {t['partitions_partial']} partial, "
                      f"{t['partitions_failed']} failed, {t['partitions_not_attempted']} not attempted. "
                      f"Boards: {t['boards_planned']:,} planned, {t['boards_scanned_in_full']:,} read in full, "
                      f"{t['boards_read_in_part']:,} in part, {t['boards_needing_recovery']:,} needing recovery "
                      f"({t['boards_deferred_provider_limits']:,} held back by provider limits). "
                      f"Coverage {t['coverage_pct']}%"
                      + (f", {t['duration_minutes']} min." if "duration_minutes" in t else ".")]
    lines += ["", "| Partition | Status | Minutes | Reason |", "|---|---|---|---|"]
    for e in coverage["partitions"]:
        lines.append(f"| {e['id']} | {e['status']} | {e.get('minutes', '')} | {e['reason']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--db", type=Path, default=Path("data/jobs.db"))
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--parts-dir", type=Path, required=True)
    ap.add_argument("--reports-dir", type=Path, default=None)
    ap.add_argument("--coverage", type=Path, required=True)
    ap.add_argument("--outcomes", type=Path, default=None, help="board-by-board outcomes (private: names boards)")
    ap.add_argument("--started", default="", help="when the run started (ISO, UTC), for its duration")
    args = ap.parse_args(argv)
    manifest = json.loads(args.manifest.read_text())
    parts = {}
    for p in manifest["partitions"]:
        found = args.parts_dir / f"part-{p['id']}" / "part.db"
        parts[p["id"]] = found if found.is_file() else None
    reports = {}
    if args.reports_dir and args.reports_dir.is_dir():
        for p in manifest["partitions"]:
            f = args.reports_dir / f"report-{p['id']}" / "report.json"
            if f.is_file():
                try:
                    reports[p["id"]] = json.loads(f.read_text())
                except ValueError:
                    reports[p["id"]] = {"unreadable": True}
    try:
        coverage = merge(args.db, manifest, parts, reports)
    except MergeError as exc:
        print(f"merge stopped: {exc}", file=sys.stderr)
        return 4
    if args.started:
        try:
            began = datetime.fromisoformat(args.started.replace("Z", "+00:00"))
            coverage["totals"] = totals(coverage, began, datetime.now(timezone.utc))
        except ValueError:
            pass
    if args.outcomes:
        args.outcomes.write_text(json.dumps(coverage["outcomes"], separators=(",", ":")))
    args.coverage.write_text(json.dumps(public(coverage), indent=1))
    print(summary(coverage))
    print(f"complete={'true' if coverage['complete_refresh'] else 'false'}")
    return 0 if coverage["complete_refresh"] else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
