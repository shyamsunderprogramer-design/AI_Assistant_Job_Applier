"""A one-table summary of the last scrape, for the GitHub Actions run page.

Counts only, never titles. This is written to $GITHUB_STEP_SUMMARY, and a step
summary on a public repository is public -- so it may say how many postings
matched, and must not say which.

Lived in the workflow as an inline heredoc until the escaped quotes inside an
f-string stopped being valid Python and the step failed on a syntax error. A
file that can be run and tested locally is the right home for it.

Not to be confused with tools/run_summary.py, which emits JSON for the local
daily pipeline's notifications and has nothing to do with Actions.

    python tools/actions_summary.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path("data/jobs.db")

LAST_RUN = ("SELECT run_id FROM scrape_log ORDER BY created_at DESC LIMIT 1")

ROWS = (
    ("Boards watched", "SELECT COUNT(*) FROM companies WHERE active=1"),
    ("Postings seen", f"SELECT COALESCE(SUM(jobs_seen), 0) FROM scrape_log "
                      f"WHERE run_id=({LAST_RUN})"),
    ("Open matches", "SELECT COUNT(*) FROM jobs WHERE is_open=1"),
    ("Found today", "SELECT COUNT(*) FROM jobs WHERE date(found_at)=date('now')"),
    ("Boards that failed", f"SELECT COUNT(*) FROM scrape_log WHERE ok=0 "
                           f"AND run_id=({LAST_RUN})"),
)


def summary(db_path: Path = DB) -> str:
    if not db_path.exists():
        return "## Daily scrape\n\nNo database — the scrape did not get that far.\n"

    db = sqlite3.connect(db_path)
    lines = ["## Daily scrape", "", "| | |", "|---|---:|"]
    for label, sql in ROWS:
        try:
            value = db.execute(sql).fetchone()[0] or 0
        except sqlite3.Error:
            value = None
        lines.append(f"| {label} | {value:,} |" if isinstance(value, int)
                     else f"| {label} | — |")
    db.close()

    lines += ["", "_Scoring runs on the laptop, where the resume is._"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(summary())
