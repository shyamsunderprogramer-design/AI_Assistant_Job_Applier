"""Find Workday boards by following companies' own careers pages.

Greenhouse, Lever and Ashby are discovered by guessing a slug from the company
name and asking the board. That does not work for Workday: a board is named by
a tenant, the datacenter it sits in and the career site's own name, and none of
the three can be guessed. A bare tenant host answers 406 whether or not the
tenant exists, so probing tells you nothing.

What does work is the company's own careers URL. A Workday shop redirects
`example.com/careers` to `example.wd3.myworkdayjobs.com/SomeSite`, and that URL
names all three parts exactly. Those careers URLs already exist — the website
enrichment collected 991 of them — and this is what they are for.

    python -m companies.discover_workday --limit 200
    python -m companies.discover_workday --retry-misses
"""

from __future__ import annotations

import argparse
import sys
import time

import requests

from . import db as cdb
from .common import UA

PROBE_SCHEMA = """
CREATE TABLE IF NOT EXISTS workday_probe (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    slug TEXT,
    found BOOLEAN NOT NULL,
    probed_at TEXT NOT NULL
);
"""

PROBE_DELAY_S = 1.0     # these are other people's servers
TIMEOUT_S = 15
# A careers page is HTML; a tenant link can sit anywhere in it, but reading a
# whole marketing site to find one is wasteful.
MAX_HTML = 400_000


def find_workday(careers_url: str) -> str | None:
    """The "tenant:dc:site" behind a careers URL, or None.

    Checks where the redirect landed first, then the page body: some sites
    redirect straight to Workday, others link to it from a careers page of
    their own.
    """
    from scraper.workday import slug_from_url

    try:
        response = requests.get(
            careers_url, headers=UA, timeout=TIMEOUT_S, allow_redirects=True
        )
    except (requests.RequestException, UnicodeError, ValueError):
        return None

    found = slug_from_url(response.url)
    if found:
        return found
    try:
        return slug_from_url(response.text[:MAX_HTML])
    except (UnicodeDecodeError, ValueError):
        return None


def run(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=250)
    ap.add_argument("--retry-misses", action="store_true",
                    help="Also re-check companies already found to have no Workday")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be added, write nothing")
    args = ap.parse_args(argv[1:])

    db = cdb.connect()
    db.executescript(PROBE_SCHEMA)

    where = "careers_url IS NOT NULL"
    if not args.retry_misses:
        # The enrichment lesson: a company with no Workday still has to be
        # remembered, or every run re-fetches the same few hundred pages that
        # were never going to answer.
        where += """ AND NOT EXISTS (
            SELECT 1 FROM workday_probe p WHERE p.company_id = companies.id
        )"""
    rows = db.execute(
        f"SELECT id, name, careers_url FROM companies WHERE {where} LIMIT ?",
        (args.limit,),
    ).fetchall()
    print(f"{len(rows)} careers pages to check (limit {args.limit})")

    from db.session import init_engine
    from config.loader import load_config
    from scraper.base import CompanyRef
    from scraper.http_client import HttpSettings, PoliteClient
    from scraper.discovery import record_probe, upsert_company
    from scraper.workday import WorkdayScraper

    cfg = load_config()
    init_engine(cfg.database_url)
    scraper = WorkdayScraper(PoliteClient(HttpSettings.from_config(cfg)))

    found = added = 0
    for index, row in enumerate(rows, 1):
        slug = find_workday(row["careers_url"])
        time.sleep(PROBE_DELAY_S)

        if slug:
            # A URL can name a tenant that no longer serves a board, so confirm
            # before adding it to the scrape list.
            live = scraper.board_exists(slug)
            if live:
                found += 1
                if not args.dry_run:
                    upsert_company(row["name"], slug, "workday", origin="discovery",
                                   board_url=scraper.board_url(slug))
                    record_probe("workday", slug, True, row["name"])
                    added += 1
                print(f"  [{found:>4}] {row['name'][:30]:32} {slug}")
            else:
                slug = None       # named a tenant, but it does not answer

        if not args.dry_run:
            db.execute("INSERT OR REPLACE INTO workday_probe VALUES (?,?,?,?)",
                       (row["id"], slug, slug is not None, cdb.utcnow()))
            db.commit()

        if index % 25 == 0:
            print(f"  ... {index}/{len(rows)} checked, {found} Workday boards found")

    verb = "would add" if args.dry_run else "added"
    print(f"done: {len(rows)} checked, {found} Workday boards found, {verb} {added}")


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
