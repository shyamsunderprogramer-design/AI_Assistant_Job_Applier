"""Move the board list in and out of the database as a plain file.

The list of boards to scrape lives in `data/jobs.db`, which is gitignored --
correctly, because that same database also holds which jobs you are tracking
and which you have applied to. But it means a fresh machine, or a GitHub
Actions runner, starts with nothing to scrape: the first cloud run walked
8 boards instead of 1,501 for exactly this reason.

So the board list gets its own file. It is safe to commit to a public
repository, and that is the point of separating it: it holds only the name and
public board address of employers who are openly advertising jobs. No postings,
no scores, no statuses, nothing about the person searching.

    python -m tools.boards export          # database -> config/boards.csv
    python -m tools.boards import          # config/boards.csv -> database
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

DEFAULT_PATH = Path("config/boards.csv")
FIELDS = ("source", "slug", "name", "board_url", "origin")


def export(path: Path = DEFAULT_PATH) -> int:
    """Write every active board to a CSV, sorted so diffs stay readable."""
    from db.models import Company
    from db.session import get_session

    path.parent.mkdir(parents=True, exist_ok=True)
    with get_session() as session:
        rows = (
            session.query(Company)
            .filter(Company.active.is_(True))
            .order_by(Company.source, Company.slug)
            .all()
        )
        data = [
            {"source": c.source, "slug": c.slug, "name": c.name or "",
             "board_url": c.board_url or "", "origin": c.origin or ""}
            for c in rows
        ]

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(data)
    return len(data)


def import_(path: Path = DEFAULT_PATH) -> tuple[int, int]:
    """Add every board in the file that the database does not already have.

    Idempotent: `upsert_company` keys on (source, slug), so running this twice
    adds nothing the second time and never duplicates a board.
    """
    from scraper.discovery import upsert_company

    if not path.exists():
        raise FileNotFoundError(
            f"No board list at {path}. Create one on a machine that has the "
            f"database:\n    python -m tools.boards export")

    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    added = 0
    for row in rows:
        source = (row.get("source") or "").strip()
        slug = (row.get("slug") or "").strip()
        if not source or not slug:
            continue
        upsert_company(
            (row.get("name") or slug).strip(), slug, source,
            origin=(row.get("origin") or "imported").strip() or "imported",
            board_url=(row.get("board_url") or "").strip() or None,
        )
        added += 1
    return added, len(rows)


def run(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=("export", "import"))
    ap.add_argument("--path", type=Path, default=DEFAULT_PATH)
    args = ap.parse_args(argv[1:])

    from config.loader import load_config
    from db.session import init_engine

    init_engine(load_config().database_url)

    if args.action == "export":
        count = export(args.path)
        print(f"{count:,} boards written to {args.path}")
    else:
        added, total = import_(args.path)
        print(f"{added:,} of {total:,} boards imported from {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
