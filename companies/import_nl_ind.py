"""Layer 2: Netherlands — IND Public Register of Recognised Sponsors.

The IND (Immigration and Naturalisation Service) registers companies allowed
to sponsor highly-skilled migrants. Download the current register from
  https://ind.nl/en/public-register-recognised-sponsors
(as CSV/TSV/Excel export if offered) and pass the path:

Usage: python -m companies.import_nl_ind <path-or-url>
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from . import db as cdb
from .common import fetch, norm_key, read_csv_rows

PAGE_URL = "https://ind.nl/en/public-register-recognised-sponsors"


def run(argv: list[str]) -> None:
    print("Netherlands IND recognised sponsors")
    if len(argv) < 2:
        raise SystemExit(
            f"pass the register file: python -m companies.import_nl_ind <path-or-url>\n"
            f"get it from {PAGE_URL}"
        )
    src = argv[1]
    path = fetch(src, "nl_ind_sponsors" + Path(src).suffix) if src.startswith("http") else Path(src)

    if path.suffix.lower() in (".xlsx", ".xls"):
        rows = pd.read_excel(path).fillna("").to_dict("records")
    else:
        rows = read_csv_rows(path)
    print(f"  {len(rows):,} rows")

    db = cdb.connect()
    inserted = matched = skipped = 0
    for row in rows:
        name = norm_key(row, "Name of organisation", "Name organisation", "Organisation",
                        "Company", "Name", "Bedrijfsnaam")
        if not name:
            skipped += 1
            continue
        city = norm_key(row, "City", "Place", "Plaats")

        cid, new = cdb.upsert_company(db, name, country="NL", city=city, tier="sponsor")
        cdb.add_sponsorship(
            db, cid, country="NL", program="Highly Skilled Migrant", status="active",
            source="nl_ind_recognised_sponsor_register",
        )
        inserted += new
        matched += not new
    cdb.log_import(db, "nl_ind_recognised_sponsor_register", len(rows), inserted, matched,
                   f"skipped={skipped}")
    db.commit()
    print(f"  rows={len(rows):,} inserted={inserted:,} matched_existing={matched:,} skipped={skipped:,}")


if __name__ == "__main__":
    sys.exit(run(sys.argv))
