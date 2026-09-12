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

CAREER_PATHS = ("/careers", "/jobs", "/en/careers", "/about/careers", "/career")
CAREER_SUBDOMAINS = ("careers", "jobs")

PROBE_DELAY_S = 0.5  # half-second between requests to the outside world


def probe(url: str) -> int | None:
    try:
        r = requests.get(url, headers=UA, timeout=10, allow_redirects=True)
        return r.status_code
    except requests.RequestException:
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
    args = ap.parse_args(argv[1:])

    db = cdb.connect()
    where = "careers_url IS NULL AND website IS NOT NULL"
    params: list = []
    if args.tier:
        where += " AND tier = ?"
        params.append(args.tier)
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
            # commit per write so parallel importers aren't lock-starved
            db.execute("UPDATE companies SET careers_url = ?, updated_at = ? WHERE id = ?",
                       (url, cdb.utcnow(), row["id"]))
            db.commit()
            found += 1
        if i % 25 == 0:
            print(f"  {i}/{len(rows)} probed, {found} careers pages found")
    db.commit()
    print(f"done: {checked} probed, {found} careers pages found ({100*found//max(checked,1)}%)")


if __name__ == "__main__":
    sys.exit(run(sys.argv))
