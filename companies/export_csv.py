"""Export the companies DB to flat CSVs in data/companies_build/export/."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from . import db as cdb

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "companies_build" / "export"


def run() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    db = cdb.connect()

    companies = pd.read_sql_query("SELECT * FROM companies", db)
    sponsorship = pd.read_sql_query(
        """SELECT s.company_id, c.name AS company, c.country AS company_country,
                  s.country AS sponsor_country, s.program, s.status,
                  s.source, s.source_detail, s.last_verified
           FROM sponsorship s JOIN companies c ON c.id = s.company_id""",
        db,
    )

    c_path = OUT_DIR / "companies.csv"
    s_path = OUT_DIR / "sponsorship.csv"
    companies.to_csv(c_path, index=False)
    sponsorship.to_csv(s_path, index=False)
    print(f"companies.csv : {len(companies):,} rows -> {c_path}")
    print(f"sponsorship.csv: {len(sponsorship):,} rows -> {s_path}")

    by_country = companies["country"].value_counts().head(15)
    print("\ncompanies by country (top 15):")
    for k, v in by_country.items():
        print(f"  {k or '?':4} {v:,}")


if __name__ == "__main__":
    sys.exit(run())
