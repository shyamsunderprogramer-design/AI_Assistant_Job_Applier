"""Layer 1: Fortune Global 500 — the stable top tier.

Source: scraped copy of the official list (2019 edition, last one published
before Fortune moved the list behind their analytics paywall), hosted at
github.com/mslitao/Fortune-Global-500. Carries website, industry, employees
and HQ — the richest open copy of the list available.
"""

from __future__ import annotations

import sys

from . import db as cdb
from .common import fetch, read_csv_rows

TSV_URL = ("https://raw.githubusercontent.com/mslitao/Fortune-Global-500/"
           "master/Data/Fortune-Global-2019.tsv")

COUNTRY_MAP = {
    "U.S.": "US", "China": "CN", "Japan": "JP", "Germany": "DE",
    "South Korea": "KR", "U.K.": "GB", "Britain": "GB", "France": "FR",
    "Switzerland": "CH", "Netherlands": "NL", "Canada": "CA",
    "Saudi Arabia": "SA", "Taiwan": "TW", "India": "IN", "Singapore": "SG",
    "Brazil": "BR", "Italy": "IT", "Spain": "ES", "Australia": "AU",
    "Mexico": "MX", "Russia": "RU", "United Arab Emirates": "AE",
    "Sweden": "SE", "Belgium": "BE", "Ireland": "IE", "Norway": "NO",
    "Denmark": "DK", "Luxembourg": "LU", "Austria": "AT", "Finland": "FI",
    "Turkey": "TR", "Thailand": "TH", "Malaysia": "MY", "Indonesia": "ID",
    "Hong Kong": "HK", "Poland": "PL", "Qatar": "QA", "Kuwait": "KW",
    "South Africa": "ZA", "Czech Republic": "CZ", "Israel": "IL",
}


def run() -> None:
    print("Fortune Global 500 (2019 scraped copy)")
    path = fetch(TSV_URL, "fortune_global_500_2019.tsv")
    rows = read_csv_rows(path)

    db = cdb.connect()
    inserted = matched = 0
    for row in rows:
        name = (row.get("Company") or "").strip()
        if not name:
            continue
        country = COUNTRY_MAP.get((row.get("Country") or "").strip())
        hq = (row.get("HQ Location") or "").strip() or None
        city = hq.split(",")[0].strip() if hq and "," in hq else hq
        employees = (row.get("Employees") or "").strip().strip('"')
        size = f"{employees} employees" if employees else None
        website = (row.get("Company Website") or "").strip() or None
        industry = (row.get("Industry") or "").strip() or None

        cid, new = cdb.upsert_company(
            db, name, country=country, city=city, website=website,
            industry=industry, size_estimate=size, tier="fortune500",
        )
        inserted += new
        matched += not new
    cdb.log_import(db, "fortune_global_500_2019", len(rows), inserted, matched, TSV_URL)
    db.commit()
    print(f"  rows={len(rows)} inserted={inserted} matched_existing={matched}")


if __name__ == "__main__":
    sys.exit(run())
