"""Turn the 7.2-million-company dump into a list worth probing.

`companies/companies_sorted.csv` holds 7,173,426 companies. Probing all of them
against three ATS platforms would be twenty-one million requests, most of them
to companies that have never used an applicant tracking system in their lives.
This picks the fraction worth asking about.

Nothing here needs a language model, and that is worth stating plainly because
it is tempting: every decision below is exact matching -- a country string, a
non-empty domain, a size band, a set difference against what has already been
probed. Plain Python does the whole 7.2M in about ten seconds. The same work
through a local model at a very optimistic 0.8 seconds per row would take
sixty-six days, and be less accurate, because "is this row's country field
equal to 'united states'" is not a question that benefits from judgement.

What the filters are, and why:

**United States only.** The search profile's location rules are US-shaped, and
a board in another country produces postings that get filtered out later
anyway -- after the request has already been made.

**Must have a domain.** The slug prober guesses a board address from the
company's name and its domain root, and the domain root is by far the better
guess: `boards.greenhouse.io/<domain-root>` is right far more often than any
transformation of a legal name. A row without one is a much weaker probe.

**Between 51 and 5,000 employees.** This band is not arbitrary -- it is the one
already recorded in `scraper/company_import.py`, and the dump shows why it
matters: 1,371,045 of the 1.77M US companies with a domain have ten employees
or fewer. A company that size does not run Greenhouse. Above the band, the
large employers are overwhelmingly on Workday, which cannot be found by
guessing a slug at all (see `scraper/workday.py`).

**Not already probed.** 85,960 names have been through the prober already.
Re-probing them is the exact waste the `probe_log` table exists to prevent.

    python -m companies.refine                      # write the names file
    python -m companies.refine --min-size 201       # only larger companies
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

SOURCE = Path("companies/companies_sorted.csv")
OUTPUT = Path("data/refined_names.txt")

# What a usable domain looks like. Deliberately strict: a probe built from a
# malformed domain fails the same way a company with no board does, so a bad
# one is not a near-miss, it is a wasted request that teaches nothing.
DOMAIN = re.compile(r"[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+")

# The dump's size ranges, in order, with the lower bound of each.
SIZE_BANDS = {
    "1 - 10": 1,
    "11 - 50": 11,
    "51 - 200": 51,
    "201 - 500": 201,
    "501 - 1000": 501,
    "1001 - 5000": 1001,
    "5001 - 10000": 5001,
    "10001+": 10001,
}

# Ordered worst-hit-rate-last, so the file is probed in the order most likely
# to find something. Mid-size companies are where Greenhouse/Lever/Ashby live.
BAND_PRIORITY = ["201 - 500", "51 - 200", "501 - 1000", "1001 - 5000",
                 "11 - 50", "5001 - 10000", "10001+", "1 - 10"]


def probed_already() -> set[str]:
    """Every name the prober has already tried, lowercased."""
    from db.models import ProbeLog
    from db.session import get_session

    with get_session() as session:
        return {
            (row.company_name or "").strip().lower()
            for row in session.query(ProbeLog).all()
            if row.company_name
        }


def known_domains() -> set[str]:
    """Domain roots already in the company universe, so we do not re-add them."""
    import sqlite3

    path = Path("data/companies.db")
    if not path.exists():
        return set()
    db = sqlite3.connect(path)
    try:
        rows = db.execute(
            "SELECT website FROM companies WHERE website IS NOT NULL AND website != ''"
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        db.close()

    out = set()
    for (site,) in rows:
        cleaned = (site or "").strip().lower()
        for prefix in ("https://", "http://", "www."):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
        cleaned = cleaned.split("/")[0]
        if cleaned:
            out.add(cleaned)
    return out


def refine(source: Path, output: Path, *, min_size: int, max_size: int,
           country: str, skip_probed: bool, limit: int | None) -> dict:
    """Write the names worth probing, best band first. Returns the counts."""
    seen_probed = probed_already() if skip_probed else set()
    seen_known = known_domains()

    csv.field_size_limit(10**7)
    buckets: dict[str, list[str]] = {band: [] for band in SIZE_BANDS}
    stats = {"rows": 0, "country": 0, "domain": 0, "size": 0,
             "already_probed": 0, "already_known": 0, "kept": 0}
    seen_domains: set[str] = set()

    with open(source, newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            stats["rows"] += 1

            if (row.get("country") or "").strip().lower() != country:
                continue
            stats["country"] += 1

            # The dump has its own typos: seventeen US rows carry a comma where
            # a dot belongs -- "www,dvc.com", "firstheritage,.net". Left alone
            # they break the name/domain split the same way a comma in a name
            # does, and a malformed domain fails silently, looking exactly like
            # a company that simply has no board.
            domain = (row.get("domain") or "").strip().lower().replace(",", ".")
            domain = ".".join(part for part in domain.split(".") if part)
            if not DOMAIN.fullmatch(domain):
                continue
            stats["domain"] += 1

            band = (row.get("size range") or "").strip()
            floor = SIZE_BANDS.get(band)
            if floor is None or floor < min_size or floor > max_size:
                continue
            stats["size"] += 1

            # The prober splits "name,domain" on the FIRST comma
            # (scraper/discovery.py:210), so a comma inside the name silently
            # steals the domain: "zagg, inc.,zagg.com" is read as name "zagg"
            # and domain " inc.,zagg.com". 14.3% of this dump's US names carry
            # a comma -- 17,071 broken probes -- and the failure is invisible,
            # because a bad domain just returns no board like any other miss.
            # The name is only used to guess a slug and to label the row, so
            # dropping the comma costs nothing.
            name = " ".join((row.get("name") or "").replace(",", " ").split())
            if not name:
                continue
            if name.lower() in seen_probed:
                stats["already_probed"] += 1
                continue
            if domain in seen_known:
                stats["already_known"] += 1
                continue
            if domain in seen_domains:
                continue                      # the dump has duplicates
            seen_domains.add(domain)

            # "name,domain" is what discover_from_names parses, and the domain
            # root is the better slug guess of the two.
            buckets[band].append(f"{name},{domain}")
            stats["kept"] += 1

    output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(output, "w", encoding="utf-8") as handle:
        handle.write("# Probe list refined from companies/companies_sorted.csv\n")
        handle.write(f"# US companies, {min_size}-{max_size} employees, with a domain,\n")
        handle.write("# not already probed. Best-hit-rate size bands first.\n")
        for band in BAND_PRIORITY:
            rows = buckets.get(band) or []
            if not rows:
                continue
            handle.write(f"\n# --- {band} employees ({len(rows):,}) ---\n")
            for line in rows:
                if limit is not None and written >= limit:
                    break
                handle.write(line + "\n")
                written += 1
            if limit is not None and written >= limit:
                break
    stats["written"] = written
    return stats


def run(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, default=SOURCE)
    ap.add_argument("--out", type=Path, default=OUTPUT)
    ap.add_argument("--min-size", type=int, default=51,
                    help="Smallest size band to keep (default 51)")
    ap.add_argument("--max-size", type=int, default=5000,
                    help="Largest size band to keep (default 5000)")
    ap.add_argument("--country", default="united states")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--include-probed", action="store_true",
                    help="Do not skip names the prober has already tried")
    args = ap.parse_args(argv[1:])

    if not args.source.exists():
        print(f"No such file: {args.source}", file=sys.stderr)
        return 1

    from config.loader import load_config
    from db.session import init_engine

    init_engine(load_config().database_url)

    stats = refine(args.source, args.out, min_size=args.min_size,
                   max_size=args.max_size, country=args.country,
                   skip_probed=not args.include_probed, limit=args.limit)

    print(f"  read              {stats['rows']:>10,}")
    print(f"  in {args.country:<14} {stats['country']:>10,}")
    print(f"  with a domain     {stats['domain']:>10,}")
    print(f"  in the size band  {stats['size']:>10,}")
    print(f"  already probed    {stats['already_probed']:>10,}  (skipped)")
    print(f"  already known     {stats['already_known']:>10,}  (skipped)")
    print(f"  WRITTEN           {stats['written']:>10,}  -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
