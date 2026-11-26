"""Each board's authoritative inventory: every posting id a complete read of it named (Nov 2026).

Why: the Mac closed a posting only when the cloud's copy of it was closed, and the cloud keeps only the postings
its search wants. A posting this Mac holds that the cloud does not (read here before the cloud-only cutover, or
outside today's search) was never closed when its board took it down: 795 such postings were still open on
26 Nov 2026 on boards the cloud had just read in full.

So each board read COMPLETELY in the cloud hands over the ids its listing named -- all of them, filtered or not
(data_engineering/scraper/runner.py, RunSummary.inventory) -- and the Mac closes a posting of that board only when
that inventory does not name it. Never from anything less:

  * only a board whose outcome in the verified result is `complete` (never partial, failed, rate-limited, timed
    out, not attempted or deferred), and whose listing was complete with the board's own durable ids (a page cap,
    an entry with no id, fewer entries than the board's own total, Workday's URL-path ids: no inventory at all);
  * only an inventory of THIS run (its run id), read by the board's own planned partition, read between the run's
    start and finish, whose digest and count match its ids -- inside the locked, signed result (jobs.db's sha256);
  * only a posting not seen after the inventory was read (an older result never closes what a newer read saw);
  * a suspicious inventory -- empty, less than half the board's previous inventory, or one that would close more
    than half of the board's open postings here -- closes only what a SECOND, independent complete read (the
    previous run's inventory, kept beside it) also did not name; until then those postings are held, not closed.

Availability and the person's decisions stay apart: a posting closed here keeps its status when a person set it
(Applied, Rejected...): only the app's own statuses become "Closed", exactly as devops/merge_cloud.py does; a job
row is never deleted; ids never change; the import's undo record covers every change (devops/cloud_undo.py).

Cloud side: `write_part` (a partition's part.db), `check_part` and `merge_part` (the join, into the saved
database's `board_inventory`, the previous inventory kept beside the newest). Mac side: `load` (checked against the
verified result), `plan` (what would change, read-only) and `apply` (inside the import's own transaction).

    python -m devops.discovery_inventory preview --cloud-db jobs.db --result result.json --local data/jobs.db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PART_TABLE = "inventory"                 # in a partition's part.db
SAVED_TABLE = "board_inventory"          # in the cloud's saved database (and so in the published jobs.db)
PART_SCHEMA = (f"CREATE TABLE {PART_TABLE} (source TEXT NOT NULL, slug TEXT NOT NULL, run_id TEXT NOT NULL, "
               "read_at TEXT NOT NULL, listed INTEGER NOT NULL, digest TEXT NOT NULL, ids BLOB NOT NULL, "
               "PRIMARY KEY (source, slug))")
SAVED_SCHEMA = (f"CREATE TABLE IF NOT EXISTS {SAVED_TABLE} (source TEXT NOT NULL, slug TEXT NOT NULL, "
                "run_id TEXT NOT NULL, read_at TEXT NOT NULL, listed INTEGER NOT NULL, digest TEXT NOT NULL, "
                "ids BLOB NOT NULL, prev_run_id TEXT, prev_read_at TEXT, prev_listed INTEGER, prev_digest TEXT, "
                "prev_ids BLOB, PRIMARY KEY (source, slug))")
MAX_IDS = 50_000                         # a board naming more is not believed (the largest seen: ~2,900)
DROP_SHARE = 0.5                         # an inventory under half the previous one is suspicious ...
DROP_MIN = 10                            # ... when the previous one named at least this many
MASS_SHARE = 0.5                         # closing more than half a board's open postings here is suspicious ...
MASS_MIN = 10                            # ... when it is at least this many
SWITCH = "cloud.reconcile_inventory"     # config.yaml: true applies; otherwise every import only previews


class InventoryError(ValueError):
    pass


# -- the ids themselves ------------------------------------------------------------------------------------------

def digest(ids) -> str:
    """sha256 over the ids, sorted, one per line: the same set gives the same digest in any order."""
    return hashlib.sha256("\n".join(sorted(str(x) for x in ids)).encode()).hexdigest()


def pack(ids) -> bytes:
    return zlib.compress(json.dumps(sorted(str(x) for x in ids), separators=(",", ":")).encode(), 9)


def unpack(blob: bytes) -> list[str]:
    ids = json.loads(zlib.decompress(blob, bufsize=1 << 20).decode())
    if not isinstance(ids, list) or not all(isinstance(x, str) and x for x in ids):
        raise InventoryError("an inventory's ids are malformed")
    return ids


def _when(stamp) -> datetime | None:
    """A stored time as aware UTC (SQLite hands back naive UTC; the cloud writes ISO with +00:00)."""
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _order(read_at, run_id: str) -> tuple:
    """Which of two reads is newer: the read time, then the run (GitHub numbers runs upward) and its attempt."""
    nums = []
    for part in str(run_id).split("-")[:2]:
        nums.append(int(part) if part.isdigit() else -1)
    return (_when(read_at) or datetime.min.replace(tzinfo=timezone.utc), *nums)


# -- cloud: a partition, then the join ---------------------------------------------------------------------------

def write_part(dst: sqlite3.Connection, inventory: dict, outcomes: dict, run_id: str) -> int:
    """A partition's inventories into its part.db: boards whose outcome is complete only. Returns how many."""
    dst.execute(PART_SCHEMA)
    rows = []
    for key, inv in sorted(inventory.items()):
        source, slug = key.split("\t", 1) if isinstance(key, str) else key
        status = (outcomes.get(f"{source}\t{slug}") or [None])[0]
        ids = list(dict.fromkeys(str(x) for x in inv["ids"]))
        if status != "complete" or len(ids) > MAX_IDS:
            continue
        rows.append((source, slug, run_id, inv["at"], len(ids), digest(ids), pack(ids)))
    dst.executemany(f"INSERT INTO {PART_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def check_part(db: sqlite3.Connection, boards: dict, run_id: str) -> tuple[set, int]:
    """(the boards whose inventory in a part.db is sound, how many inventories were not). An inventory is sound
    when its board's outcome there is complete, it is of that partition's own read, and its ids match their count
    and digest; one that is not is never taken (the boards' postings still are). An older worker's result: none."""
    good: set = set()
    bad = 0
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (PART_TABLE,)).fetchone():
        return good, bad
    for source, slug, rid, read_at, listed, dig, blob in db.execute(
            f"SELECT source, slug, run_id, read_at, listed, digest, ids FROM {PART_TABLE}"):
        try:
            ids = unpack(blob)
        except (zlib.error, ValueError, UnicodeDecodeError, TypeError):
            bad += 1
            continue
        if ((boards.get((source, slug)) or (None,))[0] != "complete" or rid != run_id or _when(read_at) is None
                or len(ids) != listed or len(set(ids)) != len(ids) or digest(ids) != dig or len(ids) > MAX_IDS):
            bad += 1
            continue
        good.add((source, slug))
    return good, bad


def merge_part(db: sqlite3.Connection, sound: set) -> int:
    """Inside the join's transaction, `part` attached: each inventory becomes its board's newest, the one it
    replaces kept as the previous (a second independent read, for the Mac's suspicious-listing rule). Never an
    older read over a newer one; the same read twice changes nothing; only `sound` boards (check_part). Returns
    the inventories taken."""
    db.execute(SAVED_SCHEMA.replace(f"EXISTS {SAVED_TABLE}", f"EXISTS main.{SAVED_TABLE}"))
    if not db.execute("SELECT 1 FROM part.sqlite_master WHERE type = 'table' AND name = ?", (PART_TABLE,)).fetchone():
        return 0
    taken = 0
    for row in db.execute(f"SELECT source, slug, run_id, read_at, listed, digest, ids FROM part.{PART_TABLE}").fetchall():
        source, slug, rid, read_at = row[:4]
        new = _when(read_at)
        if new is None or (source, slug) not in sound:
            continue                                    # check_part found it unsound: never taken
        have = db.execute(f"SELECT run_id, read_at, listed, digest, ids FROM main.{SAVED_TABLE} "
                          "WHERE source = ? AND slug = ?", (source, slug)).fetchone()
        if have is None:
            db.execute(f"INSERT INTO main.{SAVED_TABLE} (source, slug, run_id, read_at, listed, digest, ids) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", row)
        elif have[0] == rid or _order(have[1], have[0]) >= _order(read_at, rid):
            continue
        else:
            db.execute(f"UPDATE main.{SAVED_TABLE} SET run_id = ?, read_at = ?, listed = ?, digest = ?, ids = ?, "
                       "prev_run_id = ?, prev_read_at = ?, prev_listed = ?, prev_digest = ?, prev_ids = ? "
                       "WHERE source = ? AND slug = ?", (*row[2:], *have, source, slug))
        taken += 1
    return taken


# -- Mac: checked against the verified result, then planned, then applied ----------------------------------------

@dataclass
class Inventory:
    source: str
    slug: str
    run_id: str
    read_at: datetime
    ids: set
    prev_ids: set | None = None          # the previous independent complete read, when there is a sound one
    prev_read_at: datetime | None = None


def load(cloud_db: Path, result: dict, checked: dict) -> tuple[dict, dict]:
    """({(source, slug): Inventory} usable from this verified result, {why: boards} for the ones refused).
    A result from before inventories (no table) gives none: nothing is reconciled from it."""
    run = str((result.get("run") or {}).get("id") or "")
    began, ended = _when((result.get("run") or {}).get("started_at")), _when((result.get("run") or {}).get("finished_at"))
    outcome = {(s, slug): (st, pid) for s, slug, st, _e, pid in result.get("boards") or []}
    usable: dict = {}
    refused: Counter = Counter()
    con = sqlite3.connect(Path(cloud_db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (SAVED_TABLE,)).fetchone():
            return {}, {}
        rows = con.execute(f"SELECT source, slug, run_id, read_at, listed, digest, ids, prev_run_id, prev_read_at, "
                           f"prev_listed, prev_digest, prev_ids FROM {SAVED_TABLE}").fetchall()
    finally:
        con.close()
    for (source, slug, rid, read_at, listed, dig, blob, prid, pread, plisted, pdig, pblob) in rows:
        key = (source, slug)
        st, pid = outcome.get(key, (None, None))
        if not rid.startswith(f"{run}-"):
            continue                                    # an earlier run's: this result says nothing new of it
        if st != "complete" or key not in checked.get("scanned", set()):
            refused["its board was not read completely in this run"] += 1
            continue
        if not pid or not rid.endswith(f"-{pid}"):
            refused["it was not read by the board's own partition"] += 1
            continue
        when = _when(read_at)
        if when is None or (began and when < began) or (ended and when > ended):
            refused["it was not read inside this run"] += 1
            continue
        try:
            ids = unpack(blob)
        except (zlib.error, ValueError, UnicodeDecodeError):
            refused["its ids cannot be read"] += 1
            continue
        if len(ids) != listed or len(set(ids)) != len(ids) or digest(ids) != dig or len(ids) > MAX_IDS:
            refused["its ids do not match its count or digest"] += 1
            continue
        inv = Inventory(source, slug, rid, when, set(ids))
        pwhen = _when(pread)
        if prid and pblob is not None and prid.split("-")[0] != run and pwhen is not None and pwhen < when:
            try:
                prev = unpack(pblob)
                if len(prev) == plisted and digest(prev) == pdig:
                    inv.prev_ids, inv.prev_read_at = set(prev), pwhen
            except (zlib.error, ValueError, UnicodeDecodeError):
                pass                                    # no sound second read: a suspicious board is held
        usable[key] = inv
    return usable, dict(refused)


@dataclass
class Plan:
    close: list = field(default_factory=list)          # (job id, key, status, applied_at, closed_at)
    reopen: list = field(default_factory=list)         # (job id, key, status, seen_at)
    refresh: list = field(default_factory=list)        # (job id, key, seen_at)
    held: list = field(default_factory=list)           # (job id, key, status, why)
    boards: dict = field(default_factory=dict)         # (source, slug) -> {listed, open_here, close, held, suspect}
    newer_kept: int = 0                                # not closed: seen here after the inventory was read

    def keys(self) -> set:
        return {r[1] for r in self.close} | {r[1] for r in self.reopen} | {r[1] for r in self.refresh}


def plan(local: sqlite3.Connection, inventories: dict, cloud_keys: set) -> Plan:
    """What the inventories change in the local database, read-only: postings of an inventoried board that the
    cloud's own copy does not hold (the import already brings those up to date). `cloud_keys`: the cloud
    database's (source, company_slug, external_id) on those boards."""
    p = Plan()
    if not inventories:
        return p
    for (source, slug), inv in sorted(inventories.items()):
        rows = local.execute("SELECT id, external_id, is_open, status, last_seen_at, closed_at, applied_at "
                             "FROM jobs WHERE source = ? AND company_slug = ?", (source, slug)).fetchall()
        rows = [r for r in rows if (source, slug, r[1]) not in cloud_keys]
        open_here = sum(1 for r in rows if r[2])
        gone = [r for r in rows if r[2] and r[1] not in inv.ids]
        why = ""
        if not inv.ids:
            why = "its listing came back empty"
        elif inv.prev_ids is not None and len(inv.prev_ids) >= DROP_MIN and len(inv.ids) < DROP_SHARE * len(inv.prev_ids):
            why = "its listing shrank to under half its previous read"
        elif len(gone) >= MASS_MIN and len(gone) > MASS_SHARE * open_here:
            why = "it would close more than half its open postings here"
        b = {"listed": len(inv.ids), "open_here": open_here, "close": 0, "held": 0, "reopen": 0,
             "suspect": why, "second_read": inv.prev_ids is not None}
        for jid, ext, is_open, status, seen, closed_at, applied_at in rows:
            key = (source, slug, ext)
            seen_t, closed_t = _when(seen), _when(closed_at)
            if ext in inv.ids:
                if not is_open and closed_t is not None and closed_t < inv.read_at:
                    p.reopen.append((jid, key, status, inv.read_at))
                    b["reopen"] += 1
                elif is_open and (seen_t is None or seen_t < inv.read_at):
                    p.refresh.append((jid, key, inv.read_at))
                continue
            if not is_open:
                continue
            if seen_t is not None and seen_t >= inv.read_at:
                p.newer_kept += 1                       # seen after this read: an older inventory decides nothing
                continue
            if why and (inv.prev_ids is None or ext in inv.prev_ids):
                p.held.append((jid, key, status, why if inv.prev_ids is None else f"{why}; its previous read named it"))
                b["held"] += 1
                continue
            p.close.append((jid, key, status, applied_at, inv.read_at))
            b["close"] += 1
        p.boards[(source, slug)] = b
    return p


def apply(local: sqlite3.Connection, p: Plan) -> dict:
    """Inside the import's open transaction: the plan written. Statuses a person set are never touched; only the
    app's own statuses become "Closed" (and "Closed" back to the default on reopening), as merge_cloud does."""
    from data_engineering.db.models import CLOSED_STATUS, DEFAULT_STATUS, SYSTEM_STATUSES

    def stamp(t: datetime) -> str:
        return t.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")

    closed = reopened = refreshed = 0
    for jid, key, status, _applied, at in p.close:
        cur = local.execute("UPDATE jobs SET is_open = 0, closed_at = ?, exported_to_excel = 0, status = ? "
                            "WHERE id = ? AND is_open = 1",
                            (stamp(at), CLOSED_STATUS if (status or "") in SYSTEM_STATUSES else status, jid))
        closed += cur.rowcount
    for jid, key, status, at in p.reopen:
        cur = local.execute("UPDATE jobs SET is_open = 1, closed_at = NULL, exported_to_excel = 0, status = ?, "
                            "last_seen_at = ? WHERE id = ? AND is_open = 0",
                            (DEFAULT_STATUS if status == CLOSED_STATUS else status, stamp(at), jid))
        reopened += cur.rowcount
    for jid, key, at in p.refresh:
        cur = local.execute("UPDATE jobs SET last_seen_at = ? WHERE id = ? AND (last_seen_at IS NULL OR last_seen_at < ?)",
                            (stamp(at), jid, stamp(at)))
        refreshed += cur.rowcount
    return {"closed": closed, "reopened": reopened, "refreshed": refreshed, "held": len(p.held)}


def candidate_keys(local_db: Path, inventories: dict, cloud_db: Path) -> set:
    """Every local posting the inventories could touch (for the undo record, before the import)."""
    if not inventories:
        return set()
    con = sqlite3.connect(Path(local_db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        held = cloud_keys(cloud_db, inventories)
        out = set()
        for source, slug in inventories:
            for (ext,) in con.execute("SELECT external_id FROM jobs WHERE source = ? AND company_slug = ?", (source, slug)):
                if (source, slug, ext) not in held:
                    out.add((source, slug, ext))
        return out
    finally:
        con.close()


def cloud_keys(cloud_db: Path, inventories: dict) -> set:
    con = sqlite3.connect(Path(cloud_db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        keys = set()
        for source, slug in inventories:
            keys.update((source, slug, ext) for (ext,) in con.execute(
                "SELECT external_id FROM jobs WHERE source = ? AND company_slug = ?", (source, slug)))
        return keys
    finally:
        con.close()


def report(p: Plan, refused: dict, inventories: dict) -> dict:
    """The preview: counts by site and by board, acted-on postings apart. Private (names boards): stays on the Mac."""
    acted = ("Applied", "Interviewing", "Offer", "Rejected", "Not Interested", "Withdrawn")
    by_site: dict = defaultdict(lambda: {"boards": 0, "close": 0, "held": 0, "reopen": 0, "refresh": 0})
    for (source, _slug), b in p.boards.items():
        s = by_site[source]
        s["boards"] += 1
        s["close"] += b["close"]
        s["held"] += b["held"]
        s["reopen"] += b["reopen"]
    for _jid, key, _at in p.refresh:
        by_site[key[0]]["refresh"] += 1
    return {
        "inventories": len(inventories), "refused": refused,
        "would_close": len(p.close), "would_hold": len(p.held), "would_reopen": len(p.reopen),
        "would_refresh": len(p.refresh), "kept_seen_later": p.newer_kept,
        "by_site": dict(sorted(by_site.items())),
        "acted_on_closing": [{"job_id": jid, "source": key[0], "board": key[1], "status": status,
                              "applied_at": applied} for jid, key, status, applied, _at in p.close
                             if (status or "") in acted or applied],
        "acted_on_held": [{"job_id": jid, "source": key[0], "board": key[1], "status": status, "why": why}
                          for jid, key, status, why in p.held if (status or "") in acted],
        "boards": [{"source": s, "board": slug, **b} for (s, slug), b in sorted(p.boards.items())
                   if b["close"] or b["held"] or b["reopen"]],
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("preview", help="what an unpacked result's inventories would change here (read-only)")
    pv.add_argument("--cloud-db", type=Path, required=True)
    pv.add_argument("--result", type=Path, required=True)
    pv.add_argument("--local", type=Path, default=Path("data/jobs.db"))
    pv.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    from devops.cloud_results import _passphrase
    from devops.discovery_result import verify
    result = json.loads(args.result.read_text())
    checked = verify(result, args.cloud_db, _passphrase())
    inventories, refused = load(args.cloud_db, result, checked)
    held = cloud_keys(args.cloud_db, inventories)
    con = sqlite3.connect(args.local.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        p = plan(con, inventories, held)
    finally:
        con.close()
    rep = report(p, refused, inventories)
    if args.out:
        args.out.write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps({k: v for k, v in rep.items() if k not in ("boards", "acted_on_closing", "acted_on_held")},
                     indent=1, default=str))
    print(f"acted-on postings it would close: {len(rep['acted_on_closing'])}; held: {len(rep['acted_on_held'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
