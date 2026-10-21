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

    python -m devops.merge_cloud data/jobs.db.from-cloud-094936
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from data_engineering.db.models import CLOSED_STATUS, DEFAULT_STATUS, SYSTEM_STATUSES

# Statuses that mean a person decided something. Never overwritten from a
# cloud row, which has no idea any of it happened.
USER_TOUCHED = ("Applied", "Interviewing", "Offer", "Rejected", "Closed",
                "Not Interested", "Withdrawn")

# A posting's own words, as its board shows them. Employers edit postings, and
# boards say what kind of job it is (Lever's commitment, Ashby's employment
# type...): a fetch here writes both onto a posting it already holds
# (data_engineering/scraper/runner.py), so on a morning the cloud did the fetch
# the merge has to, or the copy here keeps the first words it ever read.
POSTING = ("title", "location", "description", "requirements", "application_url", "content_hash")


def _carried(row: sqlite3.Row, digest: str | None, stated_here: str | None, manual) -> tuple[dict, str]:
    """What the cloud's copy of a posting held here changes, as the fetch here would change it:
    ("edited") the employer's new words, with everything read from them read again; or ("typed")
    the board's word for the kind of job, when only that is new. A person's own corrections --
    the kind of job, the clearance and visa fields -- stay theirs (jobfields.derived_for)."""
    from backend.core.jobfields import derive, derived_for, job_type
    stated_there = row["stated_job_type"] if "stated_job_type" in row.keys() else None
    if row["content_hash"] != digest:
        stated = stated_there or stated_here
        fields = derived_for(manual, derive(row["title"], row["location"], row["description"],
                                            row["requirements"], stated))
        return {**{c: row[c] for c in POSTING}, "stated_job_type": stated, **fields,
                "exported_to_excel": 0}, "edited"
    if stated_there and stated_there != stated_here:
        changes = {"stated_job_type": stated_there}
        if not manual.job_type_manual:
            changes["job_type"] = job_type(row["title"], row["description"], row["requirements"], stated_there)
        return changes, "typed"
    return {}, ""


def _posting(row: sqlite3.Row):
    """A cloud row as the fetch here reads a posting, for the search filter."""
    from data_engineering.scraper.base import RawJob
    return RawJob(source=row["source"], company=row["company"], company_slug=row["company_slug"],
                  external_id=row["external_id"], title=row["title"], location=row["location"],
                  description=row["description"], application_url=row["application_url"],
                  requirements=row["requirements"] if "requirements" in row.keys() else None)


def merge(cloud_path: Path, local_path: Path = Path("data/jobs.db"),
          *, dry_run: bool = False, search=None) -> dict:
    """Add the cloud's new postings, and bring what their boards now say to the postings held
    here: up or taken down, edited, the kind of job. Returns what happened.

    `search`: this Mac's search (scraper.filters.resolve_filter). A new posting it rejects is
    left out, as a fetch here leaves it out: the cloud's database keeps postings read under
    every search it has ever run, and the first cloud morning (Oct 20, 2026) brought in
    Kfar Saba, Gurugram and Rome, read there in September."""
    if not cloud_path.exists():
        raise FileNotFoundError(cloud_path)

    # Merge INTO the live file rather than swapping files around. The first
    # attempt at collection replaced data/jobs.db with `mv` while the web app
    # and the probe both had it open, and SQLite does not tolerate that: their
    # connections kept pointing at an inode that no longer had a name, and the
    # next page load died with "database disk image is malformed". The file on
    # disk was fine; the running processes were reading a ghost. Writing rows
    # through a normal connection cannot cause that.
    cloud = sqlite3.connect(cloud_path)
    cloud.row_factory = sqlite3.Row
    local = sqlite3.connect(local_path)
    local.execute("PRAGMA journal_mode=WAL")   # readers do not block on a write

    columns = [r[1] for r in local.execute("PRAGMA table_info(jobs)").fetchall()]
    shared = [c for c in columns
              if c in {r[1] for r in cloud.execute("PRAGMA table_info(jobs)")}
              and c != "id"]

    # A posting is its board, its company and its id, as the table's unique key
    # says: BambooHR and Workday number each company's postings from 1, so one
    # company's posting 12 is not another's. Matched on board and id alone, a
    # new posting 12 was taken for one already here and never added.
    held = {}
    for (source, slug, external, status, is_open, seen, closed_at, digest, stated,
         type_manual, eligibility_manual) in local.execute(
            "SELECT source, company_slug, external_id, status, is_open, last_seen_at, closed_at, "
            "content_hash, stated_job_type, job_type_manual, eligibility_manual FROM jobs").fetchall():
        held[(source, slug, external)] = (status, is_open, seen, closed_at, digest, stated,
                                          SimpleNamespace(job_type_manual=type_manual,
                                                          eligibility_manual=eligibility_manual))
    where = "WHERE source = ? AND company_slug = ? AND external_id = ?"

    added = refreshed = skipped = closed = reopened = edited = typed = 0
    for row in cloud.execute("SELECT * FROM jobs").fetchall():
        key = (row["source"], row["company_slug"], row["external_id"])
        if key in held:
            # Already here. The local row is the one with the history on it --
            # its score, its status, whether a packet was built. Touch only
            # what the board says: whether the posting is still up, which on a
            # morning the cloud did the fetch nothing else here would learn,
            # last_seen_at, so lifecycle does not decide it has vanished, and
            # the posting's own words. That follows lifecycle's rules: only
            # what the cloud saw after this Mac last did, and a person's status
            # is never touched ("Applied" outlives the posting; only the app's
            # own statuses become "Closed").
            status, is_open, seen_here, closed_here, digest, stated_here, manual = held[key]
            seen_there = row["last_seen_at"] or ""
            if row["is_open"] == 0 and is_open and row["closed_at"] and (seen_here or "") < row["closed_at"]:
                if not dry_run:
                    local.execute(
                        f"UPDATE jobs SET is_open = 0, closed_at = ?, status = ?, exported_to_excel = 0 {where}",
                        (row["closed_at"], CLOSED_STATUS if (status or "") in SYSTEM_STATUSES else status, *key))
                closed += 1
                continue
            changes = {}
            if row["is_open"] and not is_open and closed_here and seen_there > closed_here:
                changes.update(is_open=1, closed_at=None, exported_to_excel=0,
                               status=DEFAULT_STATUS if status == CLOSED_STATUS else status)
                reopened += 1
            else:
                refreshed += 1
            if seen_there > (seen_here or ""):
                changes["last_seen_at"] = row["last_seen_at"]     # never back to an older sighting
            if seen_there >= (seen_here or ""):
                # The cloud's copy is the latest read of the posting (the same
                # moment: a run already merged, merged again). Read here later,
                # the copy here is newer, and stays.
                carried, why = _carried(row, digest, stated_here, manual)
                changes.update(carried)
                edited += why == "edited"
                typed += why == "typed"
            if changes and not dry_run:
                local.execute(f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in changes)} {where}",
                              [*changes.values(), *key])
            continue

        if search is not None and search.reject_reason(_posting(row)):
            skipped += 1
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
            "closed": closed, "reopened": reopened, "edited": edited, "typed": typed,
            "boards_added": boards_added}


def run(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cloud_db", type=Path)
    ap.add_argument("--local", type=Path, default=Path("data/jobs.db"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="take postings this Mac's search would leave out too")
    args = ap.parse_args(argv[1:])

    search = None
    if not args.all:
        from backend.config.loader import load_config
        from data_engineering.scraper.filters import resolve_filter
        search = resolve_filter(load_config())
    result = merge(args.cloud_db, args.local, dry_run=args.dry_run, search=search)
    verb = "would add" if args.dry_run else "added"
    print(f"  {verb} {result['added']} new postings")
    if result["skipped"]:
        print(f"  left out {result['skipped']} your search would not keep")
    print(f"  refreshed {result['refreshed']} already held")
    if result["closed"] or result["reopened"]:
        print(f"  {result['closed']} taken down from their boards, {result['reopened']} back up")
    if result["edited"] or result["typed"]:
        print(f"  {result['edited']} edited by the employer, {result['typed']} given a job type by their board")
    if result["boards_added"]:
        print(f"  {verb} {result['boards_added']} boards")
    print("  nothing local was replaced — statuses, probe memory and job-alert "
          "postings are untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
