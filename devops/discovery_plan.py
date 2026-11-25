"""Discovery partitions: every board of the GitHub fetch assigned to exactly one independent partition (Nov 2026).

The four-part fetch (devops/fetch_parts.py) split whole job SITES four ways and failed as one: a slow or
rate-limited site held back, or failed, the others. Here each job site is its own lane -- never two sites in one
partition -- and a big site's boards are split over a few partitions, balanced by how long each board itself took
last time (scrape_log), longest first. Each partition runs on its own machine, from its own copy of the saved
database, and hands back only its own boards' rows (devops/discovery_worker.py); a failed or rate-limited partition
leaves the others' work intact (devops/discovery_merge.py).

Politeness does not grow with the matrix. Each site has a budget (devops/discovery_lanes.json): the most of its boards
read at the same moment across ALL its partitions. A one-host site (Greenhouse: every board on one API host) can be
split over at most `budget` machines, each keeping the 1.5 s gap to that host (README §C5); a site with a host per
employer (Workday, BambooHR) shares its budget among its partitions. Asking for 12 or 16 partitions spreads boards over
more machines; it never raises a budget.

The plan is deterministic: the same database and lane file give the same partitions, ids and plan id, whatever the
order rows come back in. Standard library only: the planning job runs before any package is installed.

    python -m devops.discovery_plan plan --db data/jobs.db --boards-csv backend/config/boards.csv --out manifest.json
    python -m devops.discovery_plan estimate --db data/jobs.db [--target-total 12] [--budget-minutes 15]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import statistics
import sys
from pathlib import Path

SCHEMA = 1
LANES = Path(__file__).with_name("discovery_lanes.json")
DEFAULT_S = 2.0                  # a board never read before, on a site with no history either
NAME = re.compile(r"^[a-z0-9_-]+$")


def load_lanes(path: Path | None = None) -> dict:
    data = json.loads(Path(path or LANES).read_text())
    if not isinstance(data.get("lanes"), dict):
        raise ValueError("the lane file has no lanes")
    return data


def _lane(lanes: dict, provider: str) -> dict:
    found = dict(lanes["lanes"].get(provider) or {})
    found.setdefault("partitions", 1)
    found.setdefault("max_partitions", 1)
    found.setdefault("budget", int(lanes.get("default_one_partition_budget", 6)))
    found.setdefault("one_host", False)
    found.setdefault("lane", "cloud")
    found.setdefault("quota", 0)
    return found


def mac_lane(lanes: dict, provider: str) -> bool:
    """Whether the site is read on the Mac instead of in the cloud fetch (lane "mac")."""
    return _lane(lanes, provider)["lane"] == "mac"


def cap_of(lane: dict) -> int:
    """The most partitions a site may be split into: never more machines on a one-host site than its budget."""
    cap = max(1, int(lane["max_partitions"]))
    return min(cap, max(1, int(lane["budget"]))) if lane["one_host"] else cap


def workers_of(lane: dict, partitions: int, index: int = 1) -> int:
    """Boards read at once in partition `index` of `partitions`: one at a time on a one-host site; else the
    site's budget shared out whole -- 6 over 4 partitions is 2, 2, 1, 1 -- so splitting never lowers, nor raises,
    how many of its boards are read at once in total (at least one per partition)."""
    if lane["one_host"]:
        return 1
    budget, k = max(1, int(lane["budget"])), max(1, partitions)
    return max(1, budget // k + (1 if index <= budget % k else 0))


def boards(db_path: Path | None, boards_csv: Path | None = None) -> list[dict]:
    """Every eligible board: the active ones in the saved database, and the board list's ones it does not hold yet
    (a board deactivated there stays out). Each with its estimated reading time in seconds."""
    rows, seen = [], set()
    history: dict[tuple, float] = {}
    if db_path is not None and Path(db_path).exists():
        with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
            for source, slug, name, url, origin, active in db.execute(
                    "SELECT source, slug, name, board_url, origin, active FROM companies"):
                seen.add((source, slug))
                if active and NAME.match(source or ""):
                    rows.append({"source": source, "slug": slug, "name": name or slug, "board_url": url or "",
                                 "origin": origin or ""})
            has_log = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'scrape_log'").fetchone()
            if has_log:
                # Each board's latest reading time (the newest row per board), in one pass: a lookup per
                # row took 4.5 minutes on a large log.
                for source, slug, seconds in db.execute(
                        "SELECT s.source, s.company_slug, s.duration_seconds FROM scrape_log s JOIN "
                        "(SELECT MAX(id) AS id FROM scrape_log GROUP BY source, company_slug) newest "
                        "ON s.id = newest.id"):
                    if seconds is not None:
                        history[(source, slug)] = float(seconds)
    if boards_csv is not None and Path(boards_csv).exists():
        with open(boards_csv, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                key = ((row.get("source") or "").strip().lower(), (row.get("slug") or "").strip())
                if key in seen or not key[1] or not NAME.match(key[0]):
                    continue
                seen.add(key)
                rows.append({"source": key[0], "slug": key[1], "name": (row.get("name") or key[1]).strip(),
                             "board_url": (row.get("board_url") or "").strip(),
                             "origin": (row.get("origin") or "imported").strip() or "imported"})
    typical: dict[str, float] = {}
    for source in {r["source"] for r in rows}:
        known = [s for (src, _), s in history.items() if src == source]
        typical[source] = statistics.median(known) if known else DEFAULT_S
    for r in rows:
        r["est_s"] = round(history.get((r["source"], r["slug"]), typical[r["source"]]), 3)
    return rows


def _lane_minutes(costs: list[float], lane: dict, partitions: int) -> float:
    """About how long a site takes split `partitions` ways: its boards' time shared by every board read at once,
    and never less than its longest single board."""
    if not costs:
        return 0.0
    at_once = partitions if lane["one_host"] else max(partitions, int(lane["budget"]))
    return max(max(costs), sum(costs) / at_once) / 60


def counts(by_site: dict[str, list[dict]], lanes: dict, target_total: int | None = None) -> dict[str, int]:
    """How many partitions each site gets: its lane's number (never past its cap), then -- when a total is asked
    for -- one more at a time to the site that would still take longest and may grow. Deterministic."""
    out = {s: min(max(1, int(_lane(lanes, s)["partitions"])), cap_of(_lane(lanes, s))) for s in by_site}
    if target_total:
        while sum(out.values()) < target_total:
            def gain(s):
                costs, lane = [b["est_s"] for b in by_site[s]], _lane(lanes, s)
                return _lane_minutes(costs, lane, out[s]) - _lane_minutes(costs, lane, out[s] + 1)
            # Only a split that makes the site faster: within the same budget, more partitions of a site with a
            # host per employer only spread the same boards-at-once thinner.
            growable = [s for s in out if out[s] < cap_of(_lane(lanes, s)) and gain(s) > 0.05]
            if not growable:
                break
            site = max(sorted(growable), key=lambda s: _lane_minutes([b["est_s"] for b in by_site[s]],
                                                                    _lane(lanes, s), out[s]))
            out[site] += 1
    return out


def _split(site_boards: list[dict], k: int) -> list[list[dict]]:
    """Longest first into the partition with the least time so far (ties: the lower index); every board once."""
    parts: list[list[dict]] = [[] for _ in range(k)]
    load = [0.0] * k
    for board in sorted(site_boards, key=lambda b: (-b["est_s"], b["slug"])):
        i = min(range(k), key=lambda j: (load[j], j))
        parts[i].append(board)
        load[i] += board["est_s"]
    return [sorted(p, key=lambda b: b["slug"]) for p in parts]


def plan_id_of(partitions: list[dict]) -> str:
    """A fingerprint of who reads what: the same assignment, the same id."""
    canon = [[p["id"], [[b["source"], b["slug"]] for b in p["boards"]]] for p in partitions]
    return hashlib.sha256(json.dumps(canon, separators=(",", ":")).encode()).hexdigest()[:16]


def make_plan(rows: list[dict], lanes: dict, target_total: int | None = None,
              budget_minutes: float | None = None, only: set[str] | None = None) -> dict:
    """The manifest: every board in exactly one partition of its own site. `only` (partition ids or site names)
    keeps those partitions to run; the rest are listed as not to run, so a subset is never taken for a full scan."""
    by_site: dict[str, list[dict]] = {}
    on_mac: dict[str, int] = {}
    for r in rows:
        if mac_lane(lanes, r["source"]):
            on_mac[r["source"]] = on_mac.get(r["source"], 0) + 1   # read on the Mac: counted, not partitioned
            continue
        by_site.setdefault(r["source"], []).append(r)
    how_many = counts(by_site, lanes, target_total)
    partitions = []
    for site in sorted(by_site):
        lane = _lane(lanes, site)
        k = how_many[site]
        for i, group in enumerate(_split(by_site[site], k), start=1):
            if not group:
                continue
            pid = f"{site}-{i}of{k}"
            w = workers_of(lane, k, i)
            partitions.append({"id": pid, "provider": site, "index": i, "of": k, "workers": w,
                               "one_host": bool(lane["one_host"]),
                               "quota": int(lane["quota"] or 0) // k if lane["quota"] else 0,
                               "est_minutes": round(_lane_minutes([b["est_s"] for b in group], lane, 1)
                                                    if lane["one_host"] else
                                                    max(max(b["est_s"] for b in group),
                                                        sum(b["est_s"] for b in group) / w) / 60, 1),
                               "boards": group})
    for p in partitions:
        p["run"] = not only or p["id"] in only or p["provider"] in only
    budget = float(budget_minutes if budget_minutes is not None else lanes.get("time_budget_minutes", 150))
    manifest = {"schema": SCHEMA, "plan_id": plan_id_of(partitions), "budget_minutes": budget,
                "boards": sum(len(p["boards"]) for p in partitions), "partitions": partitions,
                "mac_lanes": dict(sorted(on_mac.items()))}
    check_assignment(rows, manifest, lanes)
    return manifest


def check_assignment(rows: list[dict], manifest: dict, lanes: dict) -> None:
    """Every eligible board in exactly one lane: one cloud partition of its own site, or its site's Mac lane.
    Raises ValueError otherwise -- a plan that loses or doubles a board is never run."""
    where: dict[tuple, str] = {}
    for p in manifest["partitions"]:
        for b in p["boards"]:
            key = (b["source"], b["slug"])
            if key in where:
                raise ValueError("a board is in two partitions")
            if b["source"] != p["provider"]:
                raise ValueError("a partition mixes job sites")
            where[key] = p["id"]
    eligible = {(r["source"], r["slug"]) for r in rows}
    cloud = {k for k in eligible if not mac_lane(lanes, k[0])}
    if set(where) != cloud:
        raise ValueError("the cloud partitions do not hold exactly the cloud lanes' boards")
    mac = {}
    for source, _ in eligible - cloud:
        mac[source] = mac.get(source, 0) + 1
    if mac != manifest.get("mac_lanes", {}):
        raise ValueError("the Mac lanes' counts do not match their boards")


def matrix(manifest: dict) -> list[dict]:
    """What the public workflow log may show of the plan: ids, sites and counts -- never a board."""
    return [{"id": p["id"], "provider": p["provider"], "boards": len(p["boards"]), "workers": p["workers"],
             "est_minutes": p["est_minutes"], **({"quota": p["quota"]} if p.get("quota") else {})}
            for p in manifest["partitions"] if p.get("run", True)]


def finish_estimate(partition: dict, budget_minutes: float) -> tuple[float, int]:
    """(minutes to read every board, boards read within the budget), from each board's own last time: one board
    at a time on one host, else the partition's workers taking the next board as each frees up."""
    import heapq
    workers = 1 if partition["one_host"] else max(1, partition["workers"])
    free = [0.0] * workers
    done_in, end = 0, 0.0
    for board in sorted(partition["boards"], key=lambda b: b["slug"]):
        start = heapq.heappop(free)
        stop = start + board["est_s"]
        heapq.heappush(free, stop)
        end = max(end, stop)
        if stop <= budget_minutes * 60:
            done_in += 1
    return end / 60, done_in


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("plan", "estimate"):
        p = sub.add_parser(name)
        p.add_argument("--db", type=Path, default=Path("data/jobs.db"))
        p.add_argument("--boards-csv", type=Path, default=None)
        p.add_argument("--lanes", type=Path, default=LANES)
        p.add_argument("--target-total", default="")
        p.add_argument("--budget-minutes", default="")
        p.add_argument("--only", default="", help="partition ids or job sites to run, comma-separated")
    sub.choices["plan"].add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    lanes = load_lanes(args.lanes)
    target = int(args.target_total) if str(args.target_total).strip().isdigit() else None
    budget = float(args.budget_minutes) if str(args.budget_minutes).strip() else None
    only = {x.strip() for x in args.only.split(",") if x.strip()} or None
    manifest = make_plan(boards(args.db, args.boards_csv), lanes, target, budget, only)
    if args.command == "plan":
        args.out.write_text(json.dumps(manifest, separators=(",", ":")))
        print("matrix=" + json.dumps(matrix(manifest), separators=(",", ":")))
        print(f"plan_id={manifest['plan_id']}")
        print(f"budget={int(manifest['budget_minutes'])}")
        print(f"{len(manifest['partitions'])} partitions, {manifest['boards']:,} boards, plan {manifest['plan_id']}"
              + "".join(f"; {n:,} {site} boards on the Mac lane" for site, n in manifest["mac_lanes"].items()),
              file=sys.stderr)
        return 0
    b = manifest["budget_minutes"]
    print(f"{'partition':<18}{'boards':>8}{'workers':>8}{'est min':>9}{'in ' + str(int(b)) + ' min':>12}")
    for p in manifest["partitions"]:
        total, within = finish_estimate(p, b)
        print(f"{p['id']:<18}{len(p['boards']):>8}{p['workers']:>8}{total:>9.1f}{within:>12,}")
    longest = max((finish_estimate(p, b)[0] for p in manifest["partitions"]), default=0.0)
    within = sum(finish_estimate(p, b)[1] for p in manifest["partitions"])
    print(f"longest partition about {longest:.0f} min; {within:,} of {manifest['boards']:,} boards within {b:.0f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
