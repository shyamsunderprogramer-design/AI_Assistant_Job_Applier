"""Layer 3: careers-page discovery + website verification (bounded, polite).

For each company with a website but no careers_url yet, probes a small set of
standard careers locations and records the first live one. Probing is
sequential with a delay — this runs against arbitrary company sites, and the
README's politeness rule (§C5: the cheapest request is the one never sent)
applies: results persist, so a re-run only asks companies not yet answered.

Usage:
    python -m companies.enrich_careers --tier fortune500 --limit 500
    python -m companies.enrich_careers --tier sponsor --limit 1000
"""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import urlparse

import requests

from . import db as cdb
from .common import UA

PROBE_SCHEMA = """
CREATE TABLE IF NOT EXISTS careers_probe (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    found BOOLEAN NOT NULL,
    probed_at TEXT NOT NULL
);
"""

CAREER_PATHS = ("/careers", "/jobs", "/en/careers", "/about/careers", "/career")
CAREER_SUBDOMAINS = ("careers", "jobs")

PROBE_DELAY_S = 0.5  # half-second between requests to the outside world


def probe(url: str) -> int | None:
    try:
        r = requests.get(url, headers=UA, timeout=10, allow_redirects=True)
        return r.status_code
    except (requests.RequestException, UnicodeError, ValueError):
        # UnicodeError comes from the idna encode in urllib's proxy lookup,
        # outside anything requests promises to wrap.
        return None


def root_domain(website: str) -> str | None:
    host = urlparse(website if "://" in website else f"https://{website}").netloc
    if not host:
        return None
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def find_careers(website: str) -> str | None:
    base = website.rstrip("/")
    if not base.startswith("http"):
        base = f"https://{base}"
    for path in CAREER_PATHS:
        url = base + path
        if probe(url) == 200:
            return url
        time.sleep(PROBE_DELAY_S)
    domain = root_domain(website)
    if domain:
        for sub in CAREER_SUBDOMAINS:
            url = f"https://{sub}.{domain}"
            if probe(url) == 200:
                return url
            time.sleep(PROBE_DELAY_S)
    return None


def run(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default=None, help="only this tier (default: any)")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--retry-failed", action="store_true",
                    help="Also re-probe companies whose earlier probe found nothing")
    args = ap.parse_args(argv[1:])

    db = cdb.connect()
    db.executescript(PROBE_SCHEMA)

    where = "careers_url IS NULL AND website IS NOT NULL"
    params: list = []
    if args.tier:
        where += " AND tier = ?"
        params.append(args.tier)
    if not args.retry_failed:
        # A company with no careers page keeps careers_url NULL, so without a
        # record of the attempt every round re-probes the same misses — about a
        # third of them — and never reaches companies nobody has asked yet.
        where += """ AND NOT EXISTS (
            SELECT 1 FROM careers_probe p WHERE p.company_id = companies.id
        )"""
    rows = db.execute(
        f"SELECT id, name, website FROM companies WHERE {where} LIMIT ?",
        (*params, args.limit),
    ).fetchall()
    print(f"{len(rows)} companies to probe (limit {args.limit})")

    found = checked = 0
    for i, row in enumerate(rows, 1):
        url = find_careers(row["website"])
        checked += 1
        if url:
            db.execute("UPDATE companies SET careers_url = ?, updated_at = ? WHERE id = ?",
                       (url, cdb.utcnow(), row["id"]))
            found += 1
        db.execute(
            "INSERT OR REPLACE INTO careers_probe VALUES (?,?,?)",
            (row["id"], url is not None, cdb.utcnow()),
        )
        # commit per company so parallel importers aren't lock-starved
        db.commit()
        if i % 25 == 0:
            print(f"  {i}/{len(rows)} probed, {found} careers pages found")
    db.commit()
    print(f"done: {checked} probed, {found} careers pages found ({100*found//max(checked,1)}%)")


if __name__ == "__main__":
    sys.exit(run(sys.argv))
