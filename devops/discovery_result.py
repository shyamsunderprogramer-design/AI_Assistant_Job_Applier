"""The verified discovery result: what the GitHub fetch hands the Mac, complete or not (Nov 2026, contract v1).

Before it, the Mac took a run only when it ended in success, and the run ended in success only when every board was
read in full -- so one rate-limited site sent the Mac back to reading all 29,779 boards itself. Now the join publishes,
whatever the coverage, one locked file (artifact `discovery-result-v1`): a tar of

  * jobs.db      -- the merged database, as before;
  * result.json  -- the plan (every board, in its partition), and for every board what happened to it: complete,
                    partial (read, not in full: nothing closed), failed, rate_limited, timed_out, not_attempted;
                    each partition's status and times; the run's id, attempt and times; the database's sha256; and
                    a signature over all of it, keyed from RESULTS_PASSPHRASE.

The Mac (devops/cloud_results.py) checks every bit of that before it uses any of it -- `verify` below -- and then takes
the postings of the boards read, and only those, and reads only the rest itself (devops/cloud_recovery.py). The
status stays what it is: a PARTIAL result is never called complete; `complete_refresh` is true only when COMPLETE.

Three separate statuses, never mixed: the result's validity (verify), the scan's completeness (status), and the
workflow's execution (run.execution: how the partitions' jobs ended).

    python -m devops.discovery_result build --db data/jobs.db --manifest manifest.json --outcomes outcomes.json \\
        --coverage coverage.json --out result.json [--run-id N --attempt N --started ISO --scrape-jobs success]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

CONTRACT, VERSION = "discovery-result", 1
ARTIFACT = "discovery-result-v1"
LOCKED = "discovery-result.tar.enc"
MEMBERS = ("result.json", "jobs.db")
BOARD_STATES = ("complete", "partial", "failed", "rate_limited", "timed_out", "not_attempted")
SCANNED = ("complete", "partial")
COMPLETE, PARTIAL, FAILED, NOT_ATTEMPTED = "COMPLETE", "PARTIAL", "FAILED", "NOT ATTEMPTED"


class ResultError(ValueError):
    """The result cannot be trusted: nothing of it is used."""


def _key(passphrase: str) -> bytes:
    return hashlib.sha256(f"{CONTRACT}-v{VERSION}\0{passphrase}".encode()).digest()


def _canonical(result: dict) -> bytes:
    return json.dumps({k: v for k, v in result.items() if k != "signature"}, sort_keys=True,
                      separators=(",", ":")).encode()


def sign(result: dict, passphrase: str) -> str:
    return hmac.new(_key(passphrase), _canonical(result), hashlib.sha256).hexdigest()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan_id_of(partitions: list[dict]) -> str:
    """The planner's own fingerprint of who reads what (devops/discovery_plan.plan_id_of)."""
    canon = [[p["id"], [[s, slug] for s, slug in p["boards"]]] for p in partitions]
    return hashlib.sha256(json.dumps(canon, separators=(",", ":")).encode()).hexdigest()[:16]


def partition_status(counts: dict, arrived: bool, planned: bool) -> str:
    """As devops/discovery_merge decides it, from the board counts (timed_out counts as not reached)."""
    if not arrived:
        return FAILED if planned else NOT_ATTEMPTED
    total = sum(counts.values())
    if total and counts.get("not_attempted", 0) + counts.get("timed_out", 0) == total:
        return NOT_ATTEMPTED                      # nothing read: skipped, or its time was up first
    return COMPLETE if total and counts.get("complete", 0) == total else PARTIAL


def roll_up(statuses: list[str]) -> str:
    if statuses and all(s == COMPLETE for s in statuses):
        return COMPLETE
    if statuses and all(s == NOT_ATTEMPTED for s in statuses):
        return NOT_ATTEMPTED
    if statuses and all(s in (FAILED, NOT_ATTEMPTED) for s in statuses):
        return FAILED
    return PARTIAL


DETAIL = ("listing", "listed", "relevant", "described", "pending", "skipped")


def build(db: Path, manifest: dict, outcomes: list, coverage: dict, passphrase: str, *, run_id: str = "",
          attempt: str = "", started: str = "", scrape_jobs: str = "", now: datetime | None = None,
          details: list | None = None) -> dict:
    """result.json for the merged database `db`, signed.

    Since Nov 2026, beside the contract-v1 fields and read only by a Mac that knows them (an older one ignores
    them, and they are signed with the rest): `mac_lanes`, the boards per site the plan left to the Mac's own
    lanes (counts); `details`, for each board read whose listing was incomplete or whose relevant descriptions
    are still pending, [source, slug, listing, listed, relevant, described, pending, skipped]."""
    if not passphrase:
        raise ResultError("no passphrase: the result cannot be signed")
    finished = (now or datetime.now(timezone.utc))
    parts = [{"id": p["id"], "provider": p["provider"], "run": bool(p.get("run", True)),
              "boards": [[b["source"], b["slug"]] for b in p["boards"]]} for p in manifest["partitions"]]
    by_part = {e["id"]: e for e in coverage["partitions"]}
    partitions = []
    for p in parts:
        e = by_part[p["id"]]
        partitions.append({"id": p["id"], "provider": p["provider"], "status": e["status"],
                           "arrived": e["reason"] == "", "reason": e["reason"],
                           **{k: e[k] for k in ("started_at", "finished_at", "minutes") if k in e}})
    duration = None
    if started:
        try:
            duration = round((finished - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds() / 60, 1)
        except ValueError:
            duration = None
    result = {"contract": CONTRACT, "version": VERSION,
              "run": {"id": str(run_id), "attempt": str(attempt), "started_at": started,
                      "finished_at": finished.isoformat(timespec="seconds"), "duration_minutes": duration,
                      "execution": {"scrape_jobs": scrape_jobs or "unknown"}},
              "plan_id": manifest["plan_id"], "plan": parts,
              "status": coverage["status"], "complete_refresh": coverage["status"] == COMPLETE,
              "partitions": partitions,
              "boards": [list(o) for o in outcomes],
              "totals": coverage.get("totals", {}),
              "mac_lanes": dict(manifest.get("mac_lanes") or {}),
              "details": [list(d) for d in (details or [])
                          if d[2] != "complete" or int(d[6] or 0) > 0],
              "database": {"file": "jobs.db", "sha256": sha256_of(db), "bytes": Path(db).stat().st_size}}
    result["signature"] = sign(result, passphrase)
    return result


def verify(result: dict, db: Path, passphrase: str) -> dict:
    """Every check the Mac makes before it uses a result. Returns what it may use:
    {scanned: set of boards read, incomplete: {board: status}, partitions, status, started}. Raises ResultError."""
    if not isinstance(result, dict) or result.get("contract") != CONTRACT:
        raise ResultError("not a discovery result")
    if result.get("version") != VERSION:
        raise ResultError(f"contract version {result.get('version')!r} is not one this Mac reads")
    if not passphrase:
        raise ResultError("no passphrase here to check the result's signature")
    if not hmac.compare_digest(str(result.get("signature", "")), sign(result, passphrase)):
        raise ResultError("its signature does not match: altered, or locked with another passphrase")
    dbinfo = result.get("database") or {}
    if not Path(db).is_file() or sha256_of(db) != dbinfo.get("sha256"):
        raise ResultError("its database is not the one the result describes")
    try:
        con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ResultError("its database failed its integrity check")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not {"companies", "jobs"} <= tables:
                raise ResultError("its database has no postings")
            read_at = {(s, slug): t for s, slug, t in con.execute("SELECT source, slug, last_scraped_at FROM companies")}
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise ResultError(f"its database cannot be read ({type(exc).__name__})") from exc

    plan = result.get("plan") or []
    if plan_id_of(plan) != result.get("plan_id"):
        raise ResultError("its plan does not match its plan fingerprint")
    planned: dict[tuple, str] = {}
    for p in plan:
        for s, slug in p["boards"]:
            if (s, slug) in planned:
                raise ResultError("its plan names a board twice")
            if s != p["provider"]:
                raise ResultError("its plan mixes job sites in one partition")
            planned[(s, slug)] = p["id"]
    boards = result.get("boards") or []
    seen: dict[tuple, str] = {}
    for row in boards:
        if not isinstance(row, list) or len(row) != 5:
            raise ResultError("a board's outcome is malformed")
        s, slug, st, _err, pid = row
        if st not in BOARD_STATES:
            raise ResultError(f"unknown board outcome {st!r}")
        if (s, slug) in seen:
            raise ResultError("a board's outcome is given twice")
        if planned.get((s, slug)) != pid:
            raise ResultError("a board's outcome is not in its planned partition")
        seen[(s, slug)] = st
    if set(seen) != set(planned):
        raise ResultError("its outcomes do not account for exactly the planned boards")

    # The statuses must follow from the boards -- never taken on trust from the result's own word.
    parts = {p["id"]: p for p in result.get("partitions") or []}
    if set(parts) != {p["id"] for p in plan}:
        raise ResultError("its partitions do not match its plan")
    statuses = []
    for p in plan:
        counts = {st: 0 for st in BOARD_STATES}
        for s, slug in p["boards"]:
            counts[seen[(s, slug)]] += 1
        entry = parts[p["id"]]
        if not entry.get("arrived") and any(counts[st] for st in ("complete", "partial", "failed", "rate_limited")):
            raise ResultError(f"partition {p['id']} did not arrive but has boards read")
        expected = partition_status(counts, bool(entry.get("arrived")), bool(p.get("run", True)))
        if entry.get("status") != expected:
            raise ResultError(f"partition {p['id']} says {entry.get('status')}, its boards say {expected}")
        statuses.append(expected)
    overall = roll_up(statuses)
    if result.get("status") != overall or bool(result.get("complete_refresh")) != (overall == COMPLETE):
        raise ResultError(f"the result says {result.get('status')}, its partitions say {overall}")

    started = (result.get("run") or {}).get("started_at") or ""
    began = None
    if started:
        try:
            began = datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError as exc:
            raise ResultError("its start time cannot be read") from exc
    scanned = {k for k, st in seen.items() if st in SCANNED}
    pending: dict[tuple, dict] = {}
    for row in result.get("details") or []:
        if not isinstance(row, list) or len(row) != 2 + len(DETAIL):
            raise ResultError("a board's detail is malformed")
        key = (row[0], row[1])
        if key not in scanned or key in pending:
            raise ResultError("a board's detail is not one of the boards read, or is given twice")
        d = dict(zip(DETAIL, row[2:]))
        if d["listing"] not in ("complete", "incomplete") or any(
                not isinstance(d[k], int) or d[k] < 0 for k in DETAIL[1:]):
            raise ResultError("a board's detail is malformed")
        pending[key] = d
    mac_lanes = result.get("mac_lanes") or {}
    if not isinstance(mac_lanes, dict) or any(not isinstance(n, int) or n < 0 for n in mac_lanes.values()) \
            or set(mac_lanes) & {p["provider"] for p in plan}:
        raise ResultError("its Mac lanes are malformed, or name a site the cloud also read")
    for key in scanned:
        stamp = read_at.get(key)
        if stamp is None:
            raise ResultError("a board said to be read is not in its database")
        if began is not None:
            try:
                when = datetime.fromisoformat(str(stamp))
            except ValueError as exc:
                raise ResultError("a board's read time cannot be read") from exc
            if when.tzinfo is not None:
                when = when.astimezone(timezone.utc).replace(tzinfo=None)
            if when < began:
                raise ResultError("a board said to be read in this run was last read before it began")
    return {"scanned": scanned, "incomplete": {k: st for k, st in seen.items() if st not in SCANNED},
            "status": overall, "partitions": statuses, "started": started,
            "finished": (result.get("run") or {}).get("finished_at") or "",
            "run": (result.get("run") or {}).get("id") or "", "plan_id": result["plan_id"],
            "provider_paused": {}, "pending": pending, "mac_lanes": dict(mac_lanes)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--db", type=Path, required=True)
    b.add_argument("--manifest", type=Path, required=True)
    b.add_argument("--outcomes", type=Path, required=True)
    b.add_argument("--coverage", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--run-id", default="")
    b.add_argument("--attempt", default="")
    b.add_argument("--started", default="")
    b.add_argument("--scrape-jobs", default="")
    b.add_argument("--details", type=Path, default=None,
                   help="the merge's board details (default: beside --outcomes, as the merge writes them)")
    args = ap.parse_args(argv)
    manifest = json.loads(args.manifest.read_text())
    coverage = json.loads(args.coverage.read_text())
    outcomes = json.loads(args.outcomes.read_text())
    found = args.details or args.outcomes.with_name(args.outcomes.stem + "-details.json")
    details = json.loads(found.read_text()) if found.is_file() else []
    try:
        result = build(args.db, manifest, outcomes, coverage, os.environ.get("PASSPHRASE", ""),
                       run_id=args.run_id, attempt=args.attempt, started=args.started, scrape_jobs=args.scrape_jobs,
                       details=details)
        verify(result, args.db, os.environ.get("PASSPHRASE", ""))      # never publish what the Mac would refuse
    except ResultError as exc:
        print(f"result not built: {exc}", file=sys.stderr)
        return 2
    args.out.write_text(json.dumps(result, separators=(",", ":")))
    t = result["totals"]
    print(f"result: {result['status']}, {t.get('boards_scanned_in_full', 0):,} of {t.get('boards_planned', 0):,} "
          f"boards read in full")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
