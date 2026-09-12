"""Layer 2: US — USCIS H-1B Employer Data Hub.

USCIS publishes employers with actual H-1B petition approvals (not just LCAs)
as a downloadable CSV. A company with approved petitions has demonstrably
sponsored international workers.

Data page: https://www.uscis.gov/tools/h-1b-employer-data-hub
If the default URL stops working, download the latest fiscal-year CSV manually
from that page and pass its path.

Usage: python -m companies.import_us_h1b [path-or-url-to-csv]
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import db as cdb
from .common import fetch, norm_key, read_csv_rows

DEFAULT_URL = (
    "https://www.uscis.gov/sites/default/files/document/data/"
    "h1b_datahubexport-2023.csv"
)


def run(argv: list[str]) -> None:
    print("USCIS H-1B Employer Data Hub")
    src = argv[1] if len(argv) > 1 else DEFAULT_URL
    path = fetch(src, "us_h1b_employers.csv") if src.startswith("http") else Path(src)

    rows = read_csv_rows(path)
    print(f"  {len(rows):,} rows")

    db = cdb.connect()
    inserted = matched = skipped = 0
    for row in rows:
        name = norm_key(
            row,
            "Employer", "Employer (Petitioner) Name", "Employer Name",
            "Petitioner Name", "employer_name",
        )
        if not name:
            skipped += 1
            continue
        city = norm_key(row, "City", "city")
        state = norm_key(row, "State", "state")
        naics = norm_key(row, "NAICS Code", "NAICS", "naics")
        initial = norm_key(row, "Initial Approval", "initial_approval") or "0"
        continuing = norm_key(row, "Continuing Approval", "continuing_approval") or "0"
        approvals = f"initial={initial}; continuing={continuing}"
        fy = norm_key(row, "Fiscal Year", "Fiscal Year (FY)", "fiscal_year") or ""

        cid, new = cdb.upsert_company(
            db, name, country="US", city=city, region=state, industry=naics, tier="sponsor",
        )
        cdb.add_sponsorship(
            db, cid, country="US", program="H-1B", status="active",
            source="uscis_h1b_employer_data_hub",
            source_detail=f"FY{fy} approvals={approvals}" if approvals else f"FY{fy}" or None,
        )
        inserted += new
        matched += not new
    cdb.log_import(db, "uscis_h1b_employer_data_hub", len(rows), inserted, matched,
                   f"skipped={skipped}")
    db.commit()
    print(f"  rows={len(rows):,} inserted={inserted:,} matched_existing={matched:,} skipped={skipped:,}")


if __name__ == "__main__":
    sys.exit(run(sys.argv))
