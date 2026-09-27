"""Layer 1b: stock-index constituents for countries with no public sponsor register.

Australia, Japan, Korea and the UAE publish no list of authorised visa
sponsors, so the vetting signal there is stock-exchange listing itself:
public companies are audited and real. They are imported at tier "listed"
with NO sponsorship record — unlike registry imports, listing proves
stability, not sponsorship.

Sources are Wikipedia constituent tables (which mirror the official index
compositions).
"""

from __future__ import annotations

import io
import sys

import pandas as pd
import requests

from . import db as cdb

UA = {"User-Agent": "companies-db-builder/1.0 (one-off import)"}

# (source_key, url, country, table_index, name_col, other cols of interest)
SOURCES = [
    ("kospi_200_wikipedia", "https://en.wikipedia.org/wiki/KOSPI_200", "KR", 2,
     "Company", {"GICS Sector": "industry"}),
    ("asx_200_wikipedia", "https://en.wikipedia.org/wiki/S%26P/ASX_200", "AU", 2,
     "Company", {"Sector": "industry", "Headquarters": "city",
                 "Market Capitalisation (A$)": "size"}),
    ("japan_top50_wikipedia", "https://en.wikipedia.org/wiki/List_of_companies_of_Japan", "JP", 0,
     "Company", {"Revenue (US$ million)": "size"}),
    ("uae_companies_wikipedia", "https://en.wikipedia.org/wiki/List_of_companies_of_the_United_Arab_Emirates",
     "AE", 0, "Name", {"Industry": "industry", "Sector": "sector", "Headquarters": "city"}),
]


def flatten(cols) -> list[str]:
    """Multi-index wiki headers like ('Name','Name') -> plain strings."""
    out = []
    for c in cols:
        c = str(c[1] if isinstance(c, tuple) and c[0] == c[1] else c)
        if isinstance(c, tuple):  # genuinely two-level: keep the leaf
            c = str(c[-1])
        out.append(c.replace("('", "").replace("')", "").split("', '")[-1])
    return out


def run() -> None:
    db = cdb.connect()
    for key, url, country, tidx, name_col, colmap in SOURCES:
        print(f"{key}")
        html = requests.get(url, timeout=60, headers=UA).text
        table = pd.read_html(io.StringIO(html))[tidx]
        table.columns = flatten(table.columns)

        inserted = matched = 0
        for _, row in table.iterrows():
            name = str(row.get(name_col, "")).strip()
            if not name or name.lower() == "nan":
                continue
            # skip defunct/historical entries on "List of companies of X" pages
            notes = str(row.get("Notes", "")).lower()
            if "defunct" in notes:
                continue
            kw: dict = dict(country=country, tier="listed")
            if "industry" in colmap and colmap["industry"] in row:
                v = str(row[colmap["industry"]]).strip()
                kw["industry"] = None if v.lower() == "nan" else v
            if "city" in colmap and colmap["city"] in row:
                v = str(row[colmap["city"]]).strip()
                kw["city"] = None if v.lower() == "nan" else v
            if "size" in colmap and colmap["size"] in row:
                v = str(row[colmap["size"]]).strip()
                kw["size_estimate"] = None if v.lower() == "nan" else v
            _, new = cdb.upsert_company(db, name, **kw)
            inserted += new
            matched += not new
        cdb.log_import(db, key, len(table), inserted, matched, url)
        db.commit()
        print(f"  rows={len(table)} inserted={inserted} matched_existing={matched}")


if __name__ == "__main__":
    sys.exit(run())
