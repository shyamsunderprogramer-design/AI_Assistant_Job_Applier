"""Merge a cloud scrape's postings into the local database, keeping both.

The first version of the collection step simply replaced data/jobs.db with the
cloud's copy, and that was wrong in a way that only showed up when it ran for
real. The cloud knows about postings; the laptop knows about everything else.
Replacing cost, in one command:

    postings from job alerts       35  ->     0
    boards found by the probe   1,876  -> 1,501
    postings you had acted on      37  ->     5
    probe memory              138,438  ->     0

That last line is the serious one. probe_log is what makes a three-week probe
resumable -- wiping it would have re-sent a hundred and thirty-eight thousand
requests to other people's servers to learn what was already known. And it was
paid for nine new postings, because the two databases overlap almost entirely.

The direction of trust is therefore: the local database is authoritative for
everything except the postings the cloud scraped, and even there, anything the
person has touched stays touched. A cloud row never overwrites a status, and
never resurrects a job the person closed.

    python -m tools.merge_cloud data/jobs.db.from-cloud-094936
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Statuses that mean a person decided something. Never overwritten from a
# cloud row, which has no idea any of it happened.
USER_TOUCHED = ("Applied", "Interviewing", "Offer", "Rejected", "Closed",
                "Not Interested", "Withdrawn")


def merge(cloud_path: Path, local_path: Path = Path("data/jobs.db"),
          *, dry_run: bool = False) -> dict:
    """Add the cloud's new postings. Returns what happened."""
    if not cloud_path.exists():
        raise FileNotFoundError(cloud_path)

    cloud = sqlite3.connect(cloud_path)
    cloud.row_factory = sqlite3.Row
    local = sqlite3.connect(local_path)

    columns = [r[1] for r in local.execute("PRAGMA table_info(jobs)").fetchall()]
    shared = [c for c in columns
              if c in {r[1] for r in cloud.execute("PRAGMA table_info(jobs)")}
              and c != "id"]

    held = {}
    for source, external, status in local.execute(
            "SELECT source, external_id, status FROM jobs").fetchall():
        held[(source, external)] = status

    added = refreshed = skipped = 0
    for row in cloud.execute("SELECT * FROM jobs").fetchall():
        key = (row["source"], row["external_id"])
        if key in held:
            # Already here. The local row is the one with the history on it --
            # its score, its status, whether a packet was built. Touch only
            # last_seen_at, so lifecycle does not decide it has vanished.
            if not dry_run:
                local.execute(
                    "UPDATE jobs SET last_seen_at = ? WHERE source = ? AND external_id = ?",
                    (row["last_seen_at"], row["source"], row["external_id"]))
            refreshed += 1
            continue

        if not dry_run:
            names = ", ".join(shared)
            marks = ", ".join("?" for _ in shared)
            local.execute(f"INSERT INTO jobs ({names}) VALUES ({marks})",
                          [row[c] for c in shared])
        added += 1

    # Boards the cloud discovered that we do not have. Rare, since the cloud
    # seeds from our own exported list, but free to carry across.
    boards_added = 0
    local_boards = {(r[0], r[1]) for r in
                    local.execute("SELECT source, slug FROM companies").fetchall()}
    company_cols = [r[1] for r in local.execute("PRAGMA table_info(companies)").fetchall()]
    company_shared = [c for c in company_cols
                      if c in {r[1] for r in cloud.execute("PRAGMA table_info(companies)")}
                      and c != "id"]
    for row in cloud.execute("SELECT * FROM companies").fetchall():
        if (row["source"], row["slug"]) in local_boards:
            continue
        if not dry_run:
            names = ", ".join(company_shared)
            marks = ", ".join("?" for _ in company_shared)
            local.execute(f"INSERT INTO companies ({names}) VALUES ({marks})",
                          [row[c] for c in company_shared])
        boards_added += 1

    if not dry_run:
        local.commit()
    cloud.close()
    local.close()

    return {"added": added, "refreshed": refreshed, "skipped": skipped,
            "boards_added": boards_added}


def run(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cloud_db", type=Path)
    ap.add_argument("--local", type=Path, default=Path("data/jobs.db"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv[1:])

    result = merge(args.cloud_db, args.local, dry_run=args.dry_run)
    verb = "would add" if args.dry_run else "added"
    print(f"  {verb} {result['added']} new postings")
    print(f"  refreshed {result['refreshed']} already held")
    if result["boards_added"]:
        print(f"  {verb} {result['boards_added']} boards")
    print("  nothing local was replaced — statuses, probe memory and job-alert "
          "postings are untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
