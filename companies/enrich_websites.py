"""Layer 3a: website guessing for registry companies.

The sponsor registries publish no website, so careers discovery (3b) has
nothing to probe. This guesses candidate domains from the company name and
verifies them against the live web:

  1. Strip legal suffixes (Ltd, Inc, PLC...) and punctuation -> candidate slug
  2. Try https://<slug>.<tld> for the country's TLDs (.co.uk, .com, .nl...)
  3. Require HTTP 200 AND the page title to contain the name's distinctive
     token — a bare 200 is not enough (parked domains and generic hosts
     answer 200 for anything)

Verified guesses fill companies.website; rejected slugs are remembered in
website_probe so a re-run never re-asks (README §C5 politeness rule).

Usage: python -m companies.enrich_websites --country GB --limit 300
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from urllib.parse import urlparse

import requests

from . import db as cdb
from .common import UA

PROBE_DELAY_S = 0.5

PROBE_SCHEMA = """
CREATE TABLE IF NOT EXISTS website_probe (
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    candidate TEXT NOT NULL,
    ok BOOLEAN NOT NULL,
    probed_at TEXT NOT NULL,
    PRIMARY KEY (company_id, candidate)
);
"""

# only true legal-form words — stripping "services"/"group" would mangle names
LEGAL_SUFFIXES = re.compile(
    r"\b(ltd|limited|inc|incorporated|llc|llp|plc|pty|gmbh|bv|b\.v|ag|sa|s\.a|"
    r"corp|corporation|co|company|kk|k\.k|ab|as|oy|srl|spa|sarl|pty ltd)\b\.?",
    re.IGNORECASE,
)

TLD_BY_COUNTRY = {
    "GB": ["co.uk", "com", "uk.com"],
    "US": ["com"],
    "NL": ["nl", "com"],
    # fallback country -> .com
}


def slug_candidates(name: str) -> list[str]:
    base = LEGAL_SUFFIXES.sub("", name)
    base = re.sub(r"[^a-z0-9 ]", "", base.lower())
    words = [w for w in base.split() if len(w) > 1]
    if not words:
        return []
    joined = "".join(words)
    out = [joined]
    if len(joined) > 16 and len(words) > 1:
        out.append(words[0])  # very long names often shorten to the first word
    return out


def distinctive_token(name: str) -> str | None:
    words = re.sub(r"[^a-z ]", "", name.lower()).split()
    words = [w for w in words if len(w) >= 5 and not LEGAL_SUFFIXES.search(w)]
    return words[0] if words else None


def verify(url: str, token: str | None) -> bool:
    try:
        r = requests.get(url, headers=UA, timeout=10, allow_redirects=True)
    except requests.RequestException:
        return False
    if r.status_code != 200:
        return False
    if not token or urlparse(r.url).netloc.split(".")[0] == token:
        return True
    m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.IGNORECASE | re.DOTALL)
    title = (m.group(1) if m else "").lower()
    return token in title


def run(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default=None)
    ap.add_argument("--tier", default="sponsor")
    ap.add_argument("--limit", type=int, default=300)
    args = ap.parse_args(argv[1:])

    db = cdb.connect()
    db.executescript(PROBE_SCHEMA)

    where = "website IS NULL AND tier = ?"
    params: list = [args.tier]
    if args.country:
        where += " AND country = ?"
        params.append(args.country)
    rows = db.execute(
        f"SELECT id, name, country FROM companies WHERE {where} LIMIT ?",
        (*params, args.limit),
    ).fetchall()
    print(f"{len(rows)} companies to guess websites for (limit {args.limit})")

    found = 0
    for i, row in enumerate(rows, 1):
        token = distinctive_token(row["name"])
        tlds = TLD_BY_COUNTRY.get(row["country"], ["com"])
        for slug in slug_candidates(row["name"]):
            hit = None
            for tld in tlds:
                url = f"https://{slug}.{tld}"
                ok = verify(url, token)
                db.execute(
                    "INSERT OR REPLACE INTO website_probe VALUES (?,?,?,?)",
                    (row["id"], url, ok, cdb.utcnow()),
                )
                time.sleep(PROBE_DELAY_S)
                if ok:
                    hit = url
                    break
            if hit:
                db.execute("UPDATE companies SET website = ?, updated_at = ? WHERE id = ?",
                           (hit, cdb.utcnow(), row["id"]))
                found += 1
                break
        db.commit()  # per-company commit: parallel jobs aren't lock-starved
        if i % 25 == 0:
            print(f"  {i}/{len(rows)} probed, {found} websites verified")
    print(f"done: {len(rows)} probed, {found} websites verified "
          f"({100*found//max(len(rows),1)}%)")


if __name__ == "__main__":
    sys.exit(run(sys.argv))
