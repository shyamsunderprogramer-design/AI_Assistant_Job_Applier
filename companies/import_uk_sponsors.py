"""Layer 2: UK Home Office — Register of licensed sponsors (Worker & Temporary Worker).

Official CSV published at
  https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers

A company on this register holds an active sponsor licence — real, operating,
inspected. Columns (2024+ format): Organisation Name, Town/City, County,
Type & Rating (e.g. "Worker (A rating)"), Route (e.g. "Skilled Worker").

Usage: python -m companies.import_uk_sponsors [path-or-url-to-csv]
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import db as cdb
from .common import fetch, find_gov_uk_asset, norm_key, read_csv_rows

PAGE_URL = "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"


def run(argv: list[str]) -> None:
    print("UK Home Office sponsor register")
    if len(argv) > 1:
        src = argv[1]
        path = fetch(src, "uk_sponsors.csv") if src.startswith("http") else Path(src)
    else:
        print("  resolving latest CSV from gov.uk publication page")
        path = fetch(find_gov_uk_asset(PAGE_URL, ".csv"), "uk_sponsors.csv")

    rows = read_csv_rows(path)
    print(f"  {len(rows):,} rows in register")

    db = cdb.connect()
    inserted = matched = skipped = 0
    for row in rows:
        name = norm_key(row, "Organisation Name", "Organisation name", "Name")
        if not name:
            skipped += 1
            continue
        city = norm_key(row, "Town/City", "Town", "City")
        county = norm_key(row, "County")
        rating = norm_key(row, "Type & Rating", "Type and rating") or ""
        route = norm_key(row, "Route") or "Skilled Worker"

        cid, new = cdb.upsert_company(
            db, name, country="GB", city=city, region=county, tier="sponsor",
        )
        status = "active" if "a rating" in rating.lower() or not rating else "provisional"
        cdb.add_sponsorship(
            db, cid, country="GB", program=route, status=status,
            source="uk_home_office_sponsor_register", source_detail=rating or None,
        )
        inserted += new
        matched += not new
    cdb.log_import(db, "uk_home_office_sponsor_register", len(rows), inserted, matched,
                   f"skipped={skipped}")
    db.commit()
    print(f"  rows={len(rows):,} inserted={inserted:,} matched_existing={matched:,} skipped={skipped:,}")


if __name__ == "__main__":
    sys.exit(run(sys.argv))
