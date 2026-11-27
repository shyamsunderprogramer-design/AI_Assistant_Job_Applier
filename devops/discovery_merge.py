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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

COMPLETE, PARTIAL, FAILED, NOT_ATTEMPTED = "COMPLETE", "PARTIAL", "FAILED", "NOT ATTEMPTED"
READ = ("complete", "partial")
DETAIL = ("listing", "listed", "relevant", "described", "pending", "skipped")
# The scrape log's duplicate check asks for one run's row of one board. Without this index SQLite walked the
# per-site index over the whole log for every incoming row -- most of a half-hour merge, measured by replaying a
# run's own files; about a second with it (Nov 2026). Built once, kept in the saved database.
LOG_INDEX = "CREATE INDEX IF NOT EXISTS ix_scrape_log_run_board ON scrape_log (run_id, source, company_slug)"
BOARD_STATES = ("complete", "partial", "failed", "rate_limited", "not_attempted")
KEYS = {"companies": ("source", "slug"), "jobs": ("source", "company_slug", "external_id")}
WORKABLE_FEED, WORKABLE_BOARDS = "workable-feed", "workable"      # data_engineering/scraper/workable_feed.py
DEFERRED = {"ProviderQuota": "quota", "NetworkDown": "network", "ProviderCooling": "cooling"}
OWNER_RESTRICTED = "RobotsDisallowed"      # the board owner's robots.txt says no: reported apart from failures
ROBOTS_KEEP = (200, 401, 403, 404, 410, 451)   # data_engineering/scraper/http_client.ROBOTS_KEEP
# A site cools off between runs (27 Nov 2026) when it refused nearly every board it was asked (HTTP 429 through
# every retry), or asked us to stay away (Retry-After): 6 hours after the first such run, doubling with each one
# in a row, 3 days at most; one run it answers ends it. Kept in the saved database, so a restore keeps it too.
COOL_REFUSED_SHARE = 0.9
COOL_FIRST_HOURS, COOL_MAX_HOURS = 6.0, 72.0
PROVIDER_STATE = ("CREATE TABLE IF NOT EXISTS provider_state (source TEXT PRIMARY KEY, refused_runs INTEGER NOT NULL "
                  "DEFAULT 0, cool_until TEXT, reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)")


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
        have = {r[1] for r in db.execute("PRAGMA table_info(boards)")}
        details = {}
        if set(DETAIL) <= have:          # a result from before Nov 2026's posting-level counts has none
            for row in db.execute(f"SELECT source, slug, {', '.join(DETAIL)} FROM boards WHERE listing IS NOT NULL"):
                details[(row[0], row[1])] = dict(zip(DETAIL, row[2:]))
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
        from devops.discovery_inventory import check_part as check_inventory
        sound, unsound = check_inventory(db, boards, str(meta.get("run_id") or ""))
        return None, {"boards": boards, "meta": meta, "details": details, "inventory": sound,
                      "inventory_refused": unsound}
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        return f"its result cannot be read ({type(exc).__name__})", {}
    finally:
        db.close()


def _merge_one(db: sqlite3.Connection, path: Path, boards: dict, inventory: set | None = None) -> dict:
    """One checked result into the open copy, in one transaction. Returns what it did."""
    done = {"added": 0, "updated": 0, "older_skipped": 0, "logged": 0, "inventories": 0}
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
        # Each board read completely: its inventory, the previous one kept beside it (devops/discovery_inventory.py).
        from devops.discovery_inventory import merge_part
        done["inventories"] = merge_part(db, inventory or set())
        # robots.txt answers its readers fetched: the newest per host kept; older than two days dropped.
        db.execute("CREATE TABLE IF NOT EXISTS main.robots_cache (host_root TEXT PRIMARY KEY, status INTEGER NOT NULL, "
                   "body TEXT NOT NULL, fetched_at TEXT NOT NULL)")
        if db.execute("SELECT 1 FROM part.sqlite_master WHERE type = 'table' AND name = 'robots_cache'").fetchone():
            db.execute("INSERT INTO main.robots_cache SELECT host_root, status, body, fetched_at FROM part.robots_cache "
                       "WHERE typeof(status) = 'integer' AND typeof(fetched_at) = 'text' AND true "
                       "ON CONFLICT(host_root) DO UPDATE SET status = excluded.status, body = excluded.body, "
                       "fetched_at = excluded.fetched_at WHERE excluded.fetched_at > robots_cache.fetched_at")
        db.execute("DELETE FROM main.robots_cache WHERE fetched_at < ?",
                   ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds"),))
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


def prune_log(db: sqlite3.Connection, days: float, now: datetime | None = None) -> int:
    """The cloud's scrape log kept to its last `days` days -- and each board's newest row whatever its age, which
    the planner reads for how long the board takes (devops/discovery_plan.boards). Postings and boards are never
    touched. Only the cloud's own saved copy is ever pruned (the workflow asks for it); the Mac's history is not.
    Returns the rows removed."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=float(days))).strftime("%Y-%m-%d %H:%M:%S")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scrape_log'").fetchone():
        return 0
    db.execute("BEGIN IMMEDIATE")
    try:
        # Each board's newest row (the plan's latest outcome) and its newest successful one (its reading time).
        cur = db.execute("DELETE FROM scrape_log WHERE created_at < ? AND id NOT IN "
                         "(SELECT MAX(id) FROM scrape_log GROUP BY source, company_slug) AND id NOT IN "
                         "(SELECT MAX(id) FROM scrape_log WHERE ok = 1 GROUP BY source, company_slug)", (cutoff,))
        db.execute("COMMIT")
        return cur.rowcount
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def merge(base: Path, manifest: dict, parts: dict[str, Path | None], reports: dict[str, dict] | None = None,
          now: datetime | None = None, prune_log_days: float = 0) -> dict:
    """Every usable result merged into `base` (replaced in one step); the coverage report. A partition whose
    result cannot be used, or whose merge fails, is FAILED and the rest go on. Raises MergeError, leaving `base`
    as it was, only when the merged copy itself cannot be made sound."""
    reports = reports or {}
    base = Path(base)
    # No saved copy at all -- the cache restored nothing, so not even its folder exists (found by the 26 Nov 2026
    # replay): the cold start below makes the database from the results, rather than failing to open a file there.
    base.parent.mkdir(parents=True, exist_ok=True)
    work = base.with_name(base.name + ".merging")
    if work.exists():
        work.unlink()                                   # an interrupted earlier merge: its copy is not used
    for left in _side_files(work):
        left.unlink()
    settle(base)
    cold = not base.exists()
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
                "partitions": [], "providers": {}, "boards": {s: 0 for s in BOARD_STATES}, "outcomes": [],
                "details": [], "mac_lanes": dict(manifest.get("mac_lanes") or {}), "timings": {},
                # True: no saved database -- board history, the scrape log (each site's last refusal) and earlier
                # inventories start from this run; never presented as continuous with the runs before it.
                "cold_start": cold}
    # uri=True: ATTACH then reads each result through a read-only address -- it is never written.
    db = sqlite3.connect(work.resolve().as_uri(), uri=True, isolation_level=None)
    try:
        t0 = time.monotonic()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scrape_log'").fetchone():
            db.execute(LOG_INDEX)
        coverage["timings"]["index_s"] = round(time.monotonic() - t0, 2)
        t0 = time.monotonic()
        for part in manifest["partitions"]:
            pid = part["id"]
            # Planned to run and nothing usable came back: FAILED. NOT ATTEMPTED is only a partition the plan
            # itself left out (--only) -- never a machine that went missing.
            ran = bool(part.get("run", True))
            why, found = check_part(parts.get(pid), manifest, part) if part.get("run", True) else ("not run", {})
            did = {}
            if why is None:
                try:
                    did = _merge_one(db, Path(parts[pid]), found["boards"], found.get("inventory"))
                    did["inventories_refused"] = found.get("inventory_refused", 0)
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
            for key in ("started_at", "finished_at", "minutes", "rate_limited_requests", "provider_rate_limits",
                        "requests", "retry", "min_interval_s", "longest_retry_after_s"):
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
                    d = found["details"].get(key)
                    if d and st in READ:
                        coverage["details"].append([key[0], key[1], *(d.get(k) for k in DETAIL)])
                else:
                    st, err = "not_attempted", ("PartitionFailed" if ran else "NotRun")
                coverage["outcomes"].append([key[0], key[1], st, err or "", pid])
        coverage["timings"]["merge_s"] = round(time.monotonic() - t0, 2)
        stamp = now or datetime.now(timezone.utc)
        coverage["provider_state"] = update_provider_state(db, coverage["partitions"], stamp)
        coverage["reactivated"] = reactivate_wrongly_retired(db)
        # robots.txt answers that are not definite (a 429, a 5xx...) are never kept, whatever an older reader wrote.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'robots_cache'").fetchone():
            db.execute(f"DELETE FROM robots_cache WHERE status NOT IN ({', '.join(map(str, ROBOTS_KEEP))})")
        if prune_log_days:
            t0 = time.monotonic()
            coverage["pruned_log_rows"] = prune_log(db, prune_log_days, now)
            coverage["timings"]["prune_s"] = round(time.monotonic() - t0, 2)
            t0 = time.monotonic()
            db.execute("VACUUM")
            coverage["timings"]["vacuum_s"] = round(time.monotonic() - t0, 2)
        coverage["inventory"] = board_inventory(db, stamp)
        t0 = time.monotonic()
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MergeError("the merged copy failed its integrity check; the database was left as it was")
    except BaseException:
        db.close()
        work.unlink(missing_ok=True)
        raise
    coverage["timings"]["check_s"] = round(time.monotonic() - t0, 2)
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
        mine = {e["id"] for e in entries}
        deferred = sum(1 for _, _, st, err, pid in coverage["outcomes"]
                       if pid in mine and st == "not_attempted" and err in DEFERRED)
        restricted = sum(1 for _, _, st, err, pid in coverage["outcomes"]
                         if pid in mine and st == "failed" and err == OWNER_RESTRICTED)
        coverage["providers"][site] = {"status": _roll_up([e["status"] for e in entries]),
                                       "partitions": len(entries), "boards": sum(e["boards"] for e in entries),
                                       "counts": counts, "deferred": deferred, "owner_restricted": restricted,
                                       "attempted": sum(counts[k] for k in ("complete", "partial", "failed",
                                                                            "rate_limited"))}
    statuses = [e["status"] for e in coverage["partitions"]]
    coverage["status"] = _roll_up(statuses)
    # Every result arriving is reported, but it is not a refresh: only every board read in full is.
    coverage["all_arrived"] = bool(statuses) and all(e["reason"] == "" for e in coverage["partitions"])
    coverage["complete_refresh"] = coverage["status"] == COMPLETE
    coverage["totals"] = totals(coverage)
    return coverage


def totals(coverage: dict, started: datetime | None = None, finished: datetime | None = None) -> dict:
    """The run in numbers -- counts only, safe for the public summary.

    Board level, each board in exactly one of: read in full, read in part, failed, rate-limited, deferred by its
    site's quota, deferred because the machine lost the internet, out of time, not attempted -- they add up to the
    boards planned in the cloud. Boards planned on the Mac lanes are counted beside them, never dropped. Read in
    part says why, board by board (listing incomplete; relevant descriptions pending -- a board may be both), and
    the posting level says how many postings were listed, wanted by the search, described, still pending, and
    skipped on purpose as irrelevant (never a shortfall)."""
    by = {}
    for _, _, st, err, _ in coverage["outcomes"]:
        if st == "not_attempted" and err in DEFERRED:
            st = DEFERRED[err]
        elif st == "failed" and err == OWNER_RESTRICTED:
            st = "owner_restricted"
        by[st] = by.get(st, 0) + 1
    planned = len(coverage["outcomes"])
    scanned = by.get("complete", 0) + by.get("partial", 0)
    statuses = [e["status"] for e in coverage["partitions"]]
    details = coverage.get("details") or []
    col = {k: i + 2 for i, k in enumerate(DETAIL)}
    postings = {k: sum(int(d[col[k]] or 0) for d in details)
                for k in ("listed", "relevant", "described", "pending", "skipped")}
    mac = dict(coverage.get("mac_lanes") or {})
    out = {"partitions_complete": statuses.count(COMPLETE), "partitions_partial": statuses.count(PARTIAL),
           "partitions_failed": statuses.count(FAILED), "partitions_not_attempted": statuses.count(NOT_ATTEMPTED),
           "boards_planned": planned, "boards_planned_cloud": planned, "boards_planned_mac": sum(mac.values()),
           "mac_lanes": mac,
           "boards_scanned_in_full": by.get("complete", 0), "boards_read_in_part": by.get("partial", 0),
           "boards_listing_incomplete": sum(1 for d in details if d[col["listing"]] != "complete"),
           "boards_descriptions_pending": sum(1 for d in details if int(d[col["pending"]] or 0) > 0),
           "boards_needing_recovery": planned - scanned,
           "boards_deferred_provider_limits": by.get("rate_limited", 0),
           "boards_rate_limited": by.get("rate_limited", 0),
           "boards_deferred_quota": by.get("quota", 0), "boards_deferred_network": by.get("network", 0),
           "boards_deferred_cooling": by.get("cooling", 0),
           "boards_failed": by.get("failed", 0), "boards_owner_restricted": by.get("owner_restricted", 0),
           "boards_timed_out": by.get("timed_out", 0),
           "boards_not_attempted": by.get("not_attempted", 0),
           "postings_listed": postings["listed"], "postings_relevant": postings["relevant"],
           "postings_described": postings["described"], "postings_descriptions_pending": postings["pending"],
           "postings_skipped_irrelevant": postings["skipped"],
           "coverage_pct": round(100.0 * scanned / planned, 1) if planned else 0.0}
    parts = ("boards_scanned_in_full", "boards_read_in_part", "boards_failed", "boards_owner_restricted",
             "boards_rate_limited", "boards_deferred_quota", "boards_deferred_network", "boards_deferred_cooling",
             "boards_timed_out", "boards_not_attempted")
    out["reconciles"] = sum(out[k] for k in parts) == planned
    if started and finished:
        out["duration_minutes"] = round((finished - started).total_seconds() / 60, 1)
    if coverage.get("timings"):
        out["merge_seconds"] = round(sum(coverage["timings"].values()), 1)
    inv = (coverage.get("inventory") or {}).get("total")
    if inv:
        # Against EVERY board known, retired ones included: the share never rises by dropping a board.
        out["boards_known"] = inv["known"]
        out["coverage_of_known_pct"] = round(100.0 * scanned / inv["known"], 1) if inv["known"] else 0.0
    return out


def update_provider_state(db: sqlite3.Connection, partitions: list[dict], now: datetime) -> dict:
    """Each site's cool-off between runs, from this run (see COOL_*): {site: {refused_runs, cool_until, reason}}.
    A site with a partition that did not deliver, or that asked no board (it was cooling), is left as it was."""
    db.execute(PROVIDER_STATE)
    by_site: dict[str, list[dict]] = {}
    for e in partitions:
        by_site.setdefault(e["provider"], []).append(e)
    stamp = now.isoformat(timespec="seconds")
    out = {}
    db.execute("BEGIN IMMEDIATE")
    try:
        for site, entries in sorted(by_site.items()):
            if any(e["reason"] for e in entries):
                continue
            c = {k: sum(e["counts"][k] for e in entries) for k in BOARD_STATES}
            asked = c["complete"] + c["partial"] + c["failed"] + c["rate_limited"]
            if not asked:
                continue
            row = db.execute("SELECT refused_runs FROM provider_state WHERE source = ?", (site,)).fetchone()
            runs = int(row[0]) if row else 0
            until, reason = None, ""
            if c["rate_limited"] / asked >= COOL_REFUSED_SHARE:
                runs += 1
                hours = min(COOL_FIRST_HOURS * 2 ** (runs - 1), COOL_MAX_HOURS)
                until = now + timedelta(hours=hours)
                reason = f"refused {c['rate_limited']} of {asked} boards asked ({runs} run(s) in a row)"
            else:
                runs = 0
            asked_wait = max((float(e.get("longest_retry_after_s") or 0) for e in entries), default=0.0)
            if asked_wait > 0 and (until is None or now + timedelta(seconds=asked_wait) > until):
                until = now + timedelta(seconds=asked_wait)
                reason = (reason + "; " if reason else "") + f"Retry-After {asked_wait:.0f} s"
            cool = until.isoformat(timespec="seconds") if until else None
            db.execute("INSERT INTO provider_state VALUES (?, ?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET "
                       "refused_runs = excluded.refused_runs, cool_until = excluded.cool_until, "
                       "reason = excluded.reason, updated_at = excluded.updated_at", (site, runs, cool, reason, stamp))
            out[site] = {"refused_runs": runs, "cool_until": cool, "reason": reason}
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    return out


def reactivate_wrongly_retired(db: sqlite3.Connection) -> dict:
    """Boards retired on a failure that said nothing about the board (data_engineering/scraper/runner
    .counts_toward_retiring: a refusal, the host's 5xx, the owner's robots.txt), active again with a clean count:
    asked again by the next run, and retired only if their own host says they are gone. {site: boards}."""
    from data_engineering.scraper.retire_rules import counts_toward_retiring
    if not {"error_type", "error_message", "ok"} <= set(_cols(db, "main", "scrape_log")):
        return {}
    rows = db.execute(
        "SELECT c.id, c.source, l.error_type, l.error_message FROM companies c JOIN scrape_log l ON l.id = "
        "(SELECT MAX(id) FROM scrape_log s WHERE s.source = c.source AND s.company_slug = c.slug) "
        "WHERE NOT c.active AND NOT l.ok").fetchall()
    back = [(cid, src) for cid, src, et, msg in rows if not counts_toward_retiring(et or "", msg or "")]
    out: dict[str, int] = {}
    if back:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.executemany("UPDATE companies SET active = 1, consecutive_failures = 0 WHERE id = ?",
                           [(cid,) for cid, _ in back])
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        for _, src in back:
            out[src] = out.get(src, 0) + 1
    return out


def board_inventory(db: sqlite3.Connection, now: datetime) -> dict:
    """Every board the cloud knows, retired ones included, per site and in total: known, active, deactivated,
    owner-restricted (its newest read: the owner's robots.txt said no), and read successfully in the last 7 and 30
    days (each board's newest successful row survives pruning -- prune_log). Counts only."""
    if not {"error_type", "ok", "created_at"} <= set(_cols(db, "main", "scrape_log")) or \
            not {"active", "source", "slug"} <= set(_cols(db, "main", "companies")):
        return {}
    d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    d30 = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.execute(
        "WITH newest AS (SELECT source, company_slug, MAX(id) AS id FROM scrape_log GROUP BY source, company_slug), "
        "good AS (SELECT source, company_slug, MAX(created_at) AS at FROM scrape_log WHERE ok GROUP BY source, "
        "company_slug) "
        "SELECT c.source, COUNT(*), SUM(c.active = 1), SUM(c.active = 0), "
        f"SUM(coalesce(l.error_type, '') = '{OWNER_RESTRICTED}' AND NOT coalesce(l.ok, 1)), "
        "SUM(coalesce(g.at, '') >= ?), SUM(coalesce(g.at, '') >= ?) "
        "FROM companies c LEFT JOIN newest n ON n.source = c.source AND n.company_slug = c.slug "
        "LEFT JOIN scrape_log l ON l.id = n.id "
        "LEFT JOIN good g ON g.source = c.source AND g.company_slug = c.slug GROUP BY c.source", (d7, d30)).fetchall()
    keys = ("known", "active", "deactivated", "owner_restricted", "read_7d", "read_30d")
    sites = {src: dict(zip(keys, (int(v or 0) for v in vals))) for src, *vals in rows}
    total = {k: sum(v[k] for v in sites.values()) for k in keys}
    return {"sites": sites, "total": total}


def public(coverage: dict) -> dict:
    """The coverage report as published: everything but the board-by-board outcomes and details."""
    return {k: v for k, v in coverage.items() if k not in ("outcomes", "details")}


def summary(coverage: dict) -> str:
    """Markdown for the run's public summary: sites and counts only."""
    t = coverage.get("totals") or totals(coverage)
    lines = [f"### Discovery coverage: {coverage['status']}"
             + ("" if coverage["complete_refresh"] else
                " (not a complete refresh: kept as partial; the boards not read are asked again by the next cloud runs)"),
             "", "| Site | Status | Partitions | Boards | Complete | Partial | Failed | Rate-limited | Deferred | "
             "Not attempted |", "|---|---|---|---|---|---|---|---|---|---|"]
    for site, p in coverage["providers"].items():
        c = p["counts"]
        lines.append(f"| {site} | {p['status']} | {p['partitions']} | {p['boards']:,} | {c['complete']:,} | "
                     f"{c['partial']:,} | {c['failed']:,} | {c['rate_limited']:,} | {p.get('deferred', 0):,} | "
                     f"{c['not_attempted'] - p.get('deferred', 0):,} |")
    for site, n in (coverage.get("mac_lanes") or {}).items():
        lines.append(f"| {site} | read on the Mac | - | {n:,} | | | | | | |")
    lines += ["", "| Boards (each counted once) | |", "|---|---:|",
              f"| Planned in the cloud | {t['boards_planned_cloud']:,} |",
              f"| Planned on the Mac lanes | {t['boards_planned_mac']:,} |",
              f"| Read in full (listing complete, every relevant description) | {t['boards_scanned_in_full']:,} |",
              f"| Read in part | {t['boards_read_in_part']:,} |",
              f"| Failed | {t['boards_failed']:,} |",
              f"| Owner-restricted (robots.txt) | {t.get('boards_owner_restricted', 0):,} |",
              f"| Rate-limited | {t['boards_rate_limited']:,} |",
              f"| Deferred by quota (their turn comes) | {t['boards_deferred_quota']:,} |",
              f"| Deferred: no internet | {t['boards_deferred_network']:,} |",
              f"| Deferred: site cooling off | {t.get('boards_deferred_cooling', 0):,} |",
              f"| Out of time | {t['boards_timed_out']:,} |",
              f"| Not attempted | {t['boards_not_attempted']:,} |",
              "", "| Read in part, why (a board may be both) | |", "|---|---:|",
              f"| Listing incomplete | {t['boards_listing_incomplete']:,} |",
              f"| Relevant descriptions pending | {t['boards_descriptions_pending']:,} |",
              "", "| Postings (boards read) | |", "|---|---:|",
              f"| Listed | {t['postings_listed']:,} |",
              f"| Relevant to the search | {t['postings_relevant']:,} |",
              f"| Described | {t['postings_described']:,} |",
              f"| Relevant, description pending | {t['postings_descriptions_pending']:,} |",
              f"| Skipped as irrelevant (on purpose) | {t['postings_skipped_irrelevant']:,} |",
              "", f"Coverage {t['coverage_pct']}% of the cloud's boards read"
              + (f"; {t['duration_minutes']} min" if "duration_minutes" in t else "")
              + (f"; merge {t['merge_seconds']} s" if "merge_seconds" in t else "")
              + ("" if t.get("reconciles", True) else "; THE BOARD COUNTS DO NOT ADD UP") + "."]
    feeds = coverage.get("feeds")
    if feeds is not None:
        lines += ["", "| Job-search site or alerts | Status | New | Seen again | Aged out |", "|---|---|---|---|---|"]
        if not feeds.get("arrived"):
            lines.append("| (all) | NOT ARRIVED | | | |")
        for name, c in sorted((feeds.get("sources") or {}).items()):
            lines.append(f"| {name} | {c.get('status', '')} | {c.get('new', '')} | {c.get('seen', '')} | "
                         f"{c.get('retired', '')} |")
    lines += ["", "| Partition | Status | Minutes | Reason |", "|---|---|---|---|"]
    for e in coverage["partitions"]:
        lines.append(f"| {e['id']} | {e['status']} | {e.get('minutes', '')} | {e['reason']} |")
    return "\n".join(lines) + "\n"


def merge_feeds(db: Path, feeds_dir: Path | None) -> dict:
    """The job-search sites' and alert emails' rows (devops/discovery_feeds.py) brought into the saved database, in
    one transaction. Each row is that job's newer copy of the posting -- it started from this same database and no
    board partition touches these sources -- so an existing posting takes every column but its id, and a new one is
    added. Returns the public counts: what was read, added and updated, per source; nothing when no result came."""
    report_file = feeds_dir / "feeds-report.json" if feeds_dir else None
    rows_file = feeds_dir / "feeds.db" if feeds_dir else None
    if not (report_file and report_file.is_file() and rows_file and rows_file.is_file()):
        return {"arrived": False, "read": [], "row_sources": [], "sources": {}, "added": 0, "updated": 0}
    try:
        report = json.loads(report_file.read_text())
    except ValueError:
        return {"arrived": False, "read": [], "row_sources": [], "sources": {}, "added": 0, "updated": 0}
    con = sqlite3.connect(db)
    try:
        con.execute("ATTACH DATABASE ? AS f", (str(rows_file),))
        mine = [r[1] for r in con.execute("PRAGMA main.table_info(jobs)")]
        theirs = {r[1] for r in con.execute("PRAGMA f.table_info(jobs)")}
        cols = [c for c in mine if c in theirs and c != "id"]
        boards = {r[0] for r in con.execute("SELECT DISTINCT source FROM main.companies")}
        added = updated = on_boards = 0
        key = "source = ? AND company_slug = ? AND external_id = ?"
        on_board = (f"SELECT 1 FROM main.jobs WHERE source = '{WORKABLE_BOARDS}' AND external_id = ? LIMIT 1")
        for row in con.execute(f"SELECT {', '.join(cols)} FROM f.jobs").fetchall():
            values = dict(zip(cols, row))
            if values["source"] in boards:
                continue                    # never a board's posting: those are the partitions'
            k = (values["source"], values["company_slug"], values["external_id"])
            exists = con.execute(f"SELECT 1 FROM main.jobs WHERE {key}", k).fetchone()
            if (not exists and values["source"] == WORKABLE_FEED
                    and con.execute(on_board, (values["external_id"],)).fetchone()):
                on_boards += 1              # a board read it this run: its copy is the posting
                continue
            if exists:
                con.execute(f"UPDATE main.jobs SET {', '.join(f'{c} = ?' for c in cols)} WHERE {key}",
                            [*values.values(), *k])
                updated += 1
            else:
                con.execute(f"INSERT INTO main.jobs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                            list(values.values()))
                added += 1
        # A Workable feed copy whose board copy now exists (the same shortcode) is that board's posting twice:
        # the feed copy is closed. The Mac's import keeps any status a person gave it (merge_cloud).
        superseded = con.execute(
            f"UPDATE main.jobs SET is_open = 0, closed_at = ? WHERE source = '{WORKABLE_FEED}' AND is_open = 1 "
            f"AND external_id IN (SELECT external_id FROM main.jobs WHERE source = '{WORKABLE_BOARDS}')",
            (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),)).rowcount
        con.commit()
        con.execute("DETACH DATABASE f")
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    sources = {name: {k: v for k, v in c.items() if k in ("status", "new", "seen", "retired", "calls")}
               for name, c in (report.get("sources") or {}).items()}
    return {"arrived": True, "read": sorted(report.get("read") or []),
            "row_sources": sorted(report.get("row_sources") or []), "sources": sources,
            "added": added, "updated": updated, "on_boards": on_boards, "superseded": superseded,
            "minutes": report.get("minutes")}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--db", type=Path, default=Path("data/jobs.db"))
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--parts-dir", type=Path, required=True)
    ap.add_argument("--reports-dir", type=Path, default=None)
    ap.add_argument("--coverage", type=Path, required=True)
    ap.add_argument("--outcomes", type=Path, default=None, help="board-by-board outcomes (private: names boards)")
    ap.add_argument("--started", default="", help="when the run started (ISO, UTC), for its duration")
    ap.add_argument("--feeds-dir", type=Path, default=None,
                    help="the job-search sites' result (feeds.db and feeds-report.json), merged after the boards")
    ap.add_argument("--restored-from", default="",
                    help="the run whose verified result the saved database was restored from (the cache had none)")
    ap.add_argument("--prune-log-days", type=float, default=0,
                    help="keep the scrape log to this many days (each board's newest row always kept): the cloud's copy only")
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
        coverage = merge(args.db, manifest, parts, reports, prune_log_days=args.prune_log_days)
    except MergeError as exc:
        print(f"merge stopped: {exc}", file=sys.stderr)
        return 4
    # Continuity: a saved database restored from an earlier verified result (devops/discovery_restore.py) is
    # continuous with that run, and said to be; a cold start is said by `cold_start`.
    coverage["restored_from"] = str(args.restored_from or "")
    try:
        coverage["feeds"] = merge_feeds(args.db, args.feeds_dir)
    except sqlite3.Error as exc:            # the boards' merge stands; the sites are reported unmerged
        coverage["feeds"] = {"arrived": False, "read": [], "row_sources": [], "sources": {}, "added": 0,
                             "updated": 0, "error": type(exc).__name__}
    if args.started:
        try:
            began = datetime.fromisoformat(args.started.replace("Z", "+00:00"))
            coverage["totals"] = totals(coverage, began, datetime.now(timezone.utc))
        except ValueError:
            pass
    if args.outcomes:
        args.outcomes.write_text(json.dumps(coverage["outcomes"], separators=(",", ":")))
        # Beside it, the boards read in part, posting by posting count (private too: it names boards).
        args.outcomes.with_name(args.outcomes.stem + "-details.json").write_text(
            json.dumps(coverage["details"], separators=(",", ":")))
    args.coverage.write_text(json.dumps(public(coverage), indent=1))
    print(summary(coverage))
    print(f"complete={'true' if coverage['complete_refresh'] else 'false'}")
    return 0 if coverage["complete_refresh"] else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
