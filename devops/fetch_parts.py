"""The GitHub fetch in four parts at once, and the four joined back into one database.

One fetch read every job site on one machine, six boards at a time, in about three and a half
hours. Most of that was waiting for a free place among the six, not waiting on any one site, so
the sites are split four ways and each part runs on its own machine (.github/workflows/
daily-scrape.yml). The split is by job site: every board of a site is read in one part, by one
machine, with the same 1.5 s between requests to each host as before (README §C5). Nothing is
read faster from any one site; only the queue is shorter.

The floor is the slowest single host: Greenhouse keeps all its boards on one host, about 4,200
boards, about 2 h 15 min at the polite pace. So a fetch takes about 2 h 20 min, not 50 minutes.

Each part starts from the same saved database and writes only its own sites' rows; the join
copies each site's rows from the part that read it, and from no other, and checks that every
posting a part holds is in the joined database once.

Standard library only: the planning job runs before any package is installed.

    python -m devops.fetch_parts plan --db data/jobs.db --parts 4     # parts=[...] for GitHub
    python -m devops.fetch_parts join --db data/jobs.db --plan plan.json --parts-dir parts
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

PARTS = 4
WORKERS = 6                     # limits.scrape_workers: hosts read at once on one machine
# Job sites that keep every board on one host: their boards are read one after another, so a
# site's whole time is the floor of the part it is in. The others give each employer its own
# host (acme.bamboohr.com, acme.wd5.myworkdayjobs.com) and share the part's six places.
ONE_HOST = {"greenhouse", "lever", "ashby", "workable", "jobvite"}
DEFAULT_S = 2.0                 # a board with no history: about one polite request
NAME = re.compile(r"^[a-z0-9_-]+$")


def _costs(db: sqlite3.Connection) -> dict[str, tuple[float, bool]]:
    """Each active site's reading time in seconds, from the latest run that read it, and whether
    it is on one host. A site with no history is counted by its boards."""
    sites = [r[0] for r in db.execute("SELECT DISTINCT source FROM companies WHERE active = 1")]
    costs = {}
    for site in sites:
        if not NAME.match(site or ""):
            continue
        row = db.execute(
            "SELECT run_id FROM scrape_log WHERE source = ? ORDER BY created_at DESC LIMIT 1", (site,)).fetchone()
        busy = None
        if row:
            busy = db.execute("SELECT SUM(duration_seconds) FROM scrape_log WHERE source = ? AND run_id = ?",
                              (site, row[0])).fetchone()[0]
        if not busy:
            boards = db.execute("SELECT COUNT(*) FROM companies WHERE active = 1 AND source = ?",
                                (site,)).fetchone()[0]
            busy = boards * DEFAULT_S
        costs[site] = (float(busy), site in ONE_HOST)
    return costs


def _length(sites: list[str], costs: dict) -> float:
    """How long one part takes: its slowest one-host site, or its whole reading time shared
    among the six places, whichever is longer."""
    busy = sum(costs[s][0] for s in sites)
    floor = max((costs[s][0] for s in sites if costs[s][1]), default=0.0)
    return max(floor, busy / WORKERS)


def plan(db_path: Path, parts: int = PARTS) -> list[dict]:
    """The sites split into `parts` parts, longest first into the part that stays shortest.
    The last part also takes every site not named in the others (`skip`): a site added since,
    or one the saved database has never seen, is still read, and by exactly one part."""
    with sqlite3.connect(db_path) as db:
        costs = _costs(db)
    groups: list[list[str]] = [[] for _ in range(parts)]
    for site in sorted(costs, key=lambda s: (-costs[s][0], s)):
        best = min(range(parts), key=lambda i: (_length(groups[i] + [site], costs), i))
        groups[best].append(site)
    # The last part is named by what it leaves out. An empty part is dropped: named by nothing,
    # it would read every site.
    named = [sorted(g) for g in groups[:-1] if g]
    taken = sorted(s for g in named for s in g)
    out = [{"name": str(i + 1), "sites": ",".join(g), "skip": ""} for i, g in enumerate(named)]
    out.append({"name": str(len(named) + 1), "sites": "", "skip": ",".join(taken)})
    return out


def expected_minutes(db_path: Path, parts: int = PARTS) -> float:
    """The longest part, in minutes, as the plan reckons it."""
    with sqlite3.connect(db_path) as db:
        costs = _costs(db)
    split = plan(db_path, parts)
    lengths = []
    named = {s for p in split for s in p["sites"].split(",") if s}
    for p in split:
        sites = [s for s in p["sites"].split(",") if s] or [s for s in costs if s not in named]
        lengths.append(_length(sites, costs) if sites else 0.0)
    return max(lengths, default=0.0) / 60


def owner_of(site: str, split: list[dict]) -> str:
    """The part that read `site`: the one naming it, else the one that leaves out only others."""
    for part in split:
        if site in part["sites"].split(","):
            return part["name"]
    rest = [p["name"] for p in split if not p["sites"]]
    if len(rest) != 1:
        raise ValueError("the plan has no single part for the sites it does not name")
    return rest[0]


KEYS = {"companies": ("source", "slug"), "jobs": ("source", "company_slug", "external_id")}


def _columns(db: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in db.execute(f"PRAGMA {schema}.table_info({table})")]


def join(base: Path, split: list[dict], part_dbs: dict[str, Path]) -> dict:
    """Each site's boards, postings and scrape log copied from the part that read it into `base`,
    in one transaction. Raises before changing anything when a part is missing, and rolls all of
    it back when a posting or board a part holds is not in the result exactly once."""
    names = [p["name"] for p in split]
    missing = [n for n in names if n not in part_dbs or not Path(part_dbs[n]).exists()]
    if missing:
        raise FileNotFoundError(f"part(s) {', '.join(missing)} did not arrive; nothing joined")
    db = sqlite3.connect(base, isolation_level=None)
    totals = {"added": 0, "read_again": 0, "logged": 0, "parts": len(names)}
    try:
        # All attached first: SQLite attaches nothing inside a transaction, and the join is one.
        for i, name in enumerate(names):
            db.execute(f"ATTACH DATABASE ? AS part{i}", (str(part_dbs[name]),))
        db.execute("BEGIN IMMEDIATE")
        for i, name in enumerate(names):
            part = f"part{i}"
            sites = sorted({r[0] for r in db.execute(
                f"SELECT source FROM {part}.companies UNION SELECT source FROM {part}.jobs")
                if owner_of(r[0], split) == name})
            marks = ",".join("?" for _ in sites) or "NULL"
            for table, key in KEYS.items():
                shared = [c for c in _columns(db, "main", table) if c != "id" and c in _columns(db, part, table)]
                before = db.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
                rows = db.execute(f"SELECT COUNT(*) FROM {part}.{table} WHERE source IN ({marks})", sites).fetchone()[0]
                cols = ", ".join(shared)
                sets = ", ".join(f"{c} = excluded.{c}" for c in shared if c not in key)
                # "WHERE" before ON CONFLICT is required: without it SQLite reads ON as a join.
                db.execute(f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM {part}.{table} "
                           f"WHERE source IN ({marks}) ON CONFLICT({', '.join(key)}) DO UPDATE SET {sets}", sites)
                after = db.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
                if table == "jobs":
                    totals["added"] += after - before
                    totals["read_again"] += rows - (after - before)
                # None missing and none twice: every row of the part's sites is in the result once.
                where = " AND ".join(f"m.{k} = p.{k}" for k in key)
                lost = db.execute(f"SELECT COUNT(*) FROM {part}.{table} p WHERE p.source IN ({marks}) "
                                  f"AND (SELECT COUNT(*) FROM main.{table} m WHERE {where}) != 1",
                                  sites).fetchone()[0]
                if lost:
                    raise RuntimeError(f"part {name}: {lost} {table} row(s) not joined exactly once")
            # The part's own scrape log, once: only runs the base has not seen.
            shared = [c for c in _columns(db, "main", "scrape_log") if c != "id"]
            cols = ", ".join(shared)
            done = db.execute(f"INSERT INTO main.scrape_log ({cols}) SELECT {cols} FROM {part}.scrape_log "
                              f"WHERE source IN ({marks}) AND run_id NOT IN "
                              f"(SELECT DISTINCT run_id FROM main.scrape_log)", sites)
            totals["logged"] += done.rowcount
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return totals


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--db", type=Path, default=Path("data/jobs.db"))
    p.add_argument("--parts", type=int, default=PARTS)
    j = sub.add_parser("join")
    j.add_argument("--db", type=Path, default=Path("data/jobs.db"))
    j.add_argument("--plan", type=Path, required=True)
    j.add_argument("--parts-dir", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.command == "plan":
        split = plan(args.db, args.parts) if args.db.exists() else [{"name": "1", "sites": "", "skip": ""}]
        print("parts=" + json.dumps(split, separators=(",", ":")))
        if args.db.exists():
            print(f"longest part about {expected_minutes(args.db, args.parts):.0f} min", file=sys.stderr)
        return 0
    split = json.loads(args.plan.read_text())
    found = {p["name"]: args.parts_dir / f"part-{p['name']}" / "part.db" for p in split}
    if not args.db.exists() and found and Path(next(iter(found.values()))).exists():
        # No saved copy (a cold cache): each part began from an empty one, so any part, less its
        # own sites' postings -- which the join puts back from it -- is that empty start.
        import shutil
        args.db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(next(iter(found.values())), args.db)
    got = join(args.db, split, found)
    print(f"joined {got['parts']} parts: {got['added']:,} new postings, {got['read_again']:,} read again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
