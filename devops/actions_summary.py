"""A short summary of the last fetch, for the GitHub Actions run page.

Counts only, never titles or boards. This is written to $GITHUB_STEP_SUMMARY, and a step summary on a public
repository is public -- so it may say how many postings matched, and must not say which.

The board counts come from ONE place: the run's own coverage report (coverage.json, devops/discovery_merge.py),
the same numbers the Mac acts on. Until Nov 2026 this counted "Boards that failed" from every scrape-log row of
the last seven hours, which mixed two runs and counted a board once per retry; that counter is gone. Without a coverage report (the merge never ran) it says so instead of guessing.

Not to be confused with devops/run_summary.py, which emits JSON for the local daily pipeline's notifications.

    python devops/actions_summary.py [coverage.json]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB = Path("data/jobs.db")
COVERAGE = Path("coverage.json")

ROWS = (
    ("Boards watched", "SELECT COUNT(*) FROM companies WHERE active=1"),
    ("Open matches", "SELECT COUNT(*) FROM jobs WHERE is_open=1"),
    ("Found today", "SELECT COUNT(*) FROM jobs WHERE date(found_at)=date('now')"),
)

FROM_COVERAGE = (
    ("Boards planned in the cloud", "boards_planned_cloud"),
    ("Boards planned on the Mac lanes", "boards_planned_mac"),
    ("Read in full", "boards_scanned_in_full"),
    ("Read in part", "boards_read_in_part"),
    ("Failed", "boards_failed"),
    ("Rate-limited", "boards_rate_limited"),
    ("Deferred by quota", "boards_deferred_quota"),
    ("Deferred: no internet", "boards_deferred_network"),
    ("Out of time", "boards_timed_out"),
    ("Not attempted", "boards_not_attempted"),
    ("Postings listed", "postings_listed"),
    ("Relevant descriptions pending", "postings_descriptions_pending"),
    ("Skipped as irrelevant (on purpose)", "postings_skipped_irrelevant"),
)


def summary(db_path: Path = DB, coverage_path: Path = COVERAGE) -> str:
    lines = ["## Daily scrape", "", "| | |", "|---|---:|"]
    if db_path.exists():
        db = sqlite3.connect(db_path)
        for label, sql in ROWS:
            try:
                value = db.execute(sql).fetchone()[0] or 0
            except sqlite3.Error:
                value = None
            lines.append(f"| {label} | {value:,} |" if isinstance(value, int) else f"| {label} | — |")
        db.close()
    else:
        lines.append("| Database | none — the scrape did not get that far |")
    totals = {}
    try:
        totals = (json.loads(coverage_path.read_text()) or {}).get("totals") or {}
    except (OSError, ValueError):
        totals = {}
    if totals:
        for label, key in FROM_COVERAGE:
            if key in totals:
                lines.append(f"| {label} | {int(totals[key]):,} |")
    else:
        lines.append("| Board coverage | no coverage report — see the merge step |")
    lines += ["", "_Board counts are the coverage report's; scoring runs on the laptop, where the resume is._"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(summary(coverage_path=Path(sys.argv[1]) if len(sys.argv) > 1 else COVERAGE))
