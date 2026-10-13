"""The companies with 1–10 employees, the likeliest to have a job board first.

The first pass (data/refined_names_worthwhile.txt) checked every US company in
the dump with a domain and 11 or more employees: 393,823 names, 11,332 boards.
What is left of the 1.75 million is 1.36 million companies of one to ten
people. Few of them run an applicant tracking system, so the order matters:
probed top to bottom, the list should spend its first days where boards are.

The order is measured, not guessed. Among the 11–50 employee companies already
checked, the share with a board differs by industry (software and internet
companies use Greenhouse and Lever; restaurants rarely use anything). Each
1–10 company is ranked by its industry's measured share, then by the dump's
own estimate of its headcount (a company of nine is likelier than one of one).

    python -m data_engineering.companies.small_list          # writes data/refined_names_small.txt
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

from data_engineering.companies.refine import DOMAIN, SOURCE, known_domains

OUTPUT = Path("data/refined_names_small.txt")
DONE_LIST = Path("data/refined_names_worthwhile.txt")
MEASURE_BAND = "11 - 50"           # the nearest size already checked
SMALL_BAND = "1 - 10"
MIN_MEASURED = 200                 # an industry with fewer checked companies takes the overall share


def _row(row: dict) -> tuple[str, str] | None:
    """(name, domain) as the refine step writes them, or None for a row it would skip."""
    domain = (row.get("domain") or "").strip().lower().replace(",", ".")
    domain = ".".join(part for part in domain.split(".") if part)
    if not DOMAIN.fullmatch(domain):
        return None
    name = " ".join((row.get("name") or "").replace(",", " ").split())
    return (name, domain) if name else None


def _domains_in(path: Path) -> set[str]:
    out = set()
    if path.exists():
        for line in path.open(encoding="utf-8"):
            if line.strip() and not line.startswith("#"):
                out.add(line.rstrip("\n").rpartition(",")[2].strip().lower())
    return out


def build(source: Path = SOURCE, output: Path = OUTPUT, country: str = "united states") -> dict:
    from backend.config.loader import load_config
    from data_engineering.db.session import init_engine
    from data_engineering.scraper.discovery import CompanyDiscoverer, load_probe_cache
    from data_engineering.scraper.http_client import HttpSettings, PoliteClient
    from data_engineering.scraper.runner import build_scrapers

    cfg = load_config()
    init_engine(cfg.database_url)
    cache = load_probe_cache()
    discoverer = CompanyDiscoverer(scrapers=build_scrapers(cfg, PoliteClient(HttpSettings.from_config(cfg))),
                                   strip_suffixes=cfg.get("discovery.strip_suffixes", []),
                                   single_variant=set(cfg.get("discovery.single_variant_sources", [])))
    sources = cfg.get("discovery.probe_sources", [])
    skip = _domains_in(DONE_LIST) | known_domains()

    measured: dict[str, list[int]] = defaultdict(lambda: [0, 0])     # industry -> [checked, with a board]
    small: list[tuple[str, int, str, str]] = []                       # industry, estimate, name, domain
    seen: set[str] = set()
    csv.field_size_limit(10**7)
    with open(source, newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            if (row.get("country") or "").strip().lower() != country:
                continue
            band = (row.get("size range") or "").strip()
            if band not in (MEASURE_BAND, SMALL_BAND):
                continue
            got = _row(row)
            if got is None:
                continue
            name, domain = got
            industry = (row.get("industry") or "").strip().lower() or "(none)"
            if band == MEASURE_BAND:
                pairs = discoverer.plan(name, sources, domain=domain, max_slugs=1)
                if pairs and all(p in cache for p in pairs if p[0] != "workable"):
                    measured[industry][0] += 1
                    measured[industry][1] += any(cache.get(p) for p in pairs)
                continue
            if domain in skip or domain in seen:
                continue
            seen.add(domain)
            try:
                estimate = int(float(row.get("current employee estimate") or 0))
            except ValueError:
                estimate = 0
            small.append((industry, estimate, name, domain))

    checked = sum(c for c, _ in measured.values())
    overall = sum(h for _, h in measured.values()) / checked if checked else 0.0
    rate = {ind: (h / c if c >= MIN_MEASURED else overall) for ind, (c, h) in measured.items()}
    small.sort(key=lambda r: (-rate.get(r[0], overall), -r[1]))

    output.parent.mkdir(parents=True, exist_ok=True)
    tier_of = lambda r: rate.get(r[0], overall)       # noqa: E731
    with open(output, "w", encoding="utf-8") as handle:
        handle.write("# US companies with 1-10 employees and a domain, not in the first pass.\n")
        handle.write("# Ordered by how often an 11-50 employee company in the same industry had a board,\n")
        handle.write("# then by headcount. Written by data_engineering/companies/small_list.py\n")
        current = None
        for r in small:
            share = round(100 * tier_of(r), 1)
            if share != current:
                current = share
                handle.write(f"\n# --- industries where {share}% of 11-50 employee companies had a board ---\n")
            handle.write(f"{r[2]},{r[3]}\n")

    # What it is likely to find: each industry's measured share, halved, because
    # 11-50 companies had boards half as often as 51-200 ones (2.6% against 5.2%).
    expected = sum(tier_of(r) for r in small) / 2
    top = sorted(((rate[i], c, i) for i, (c, _) in measured.items() if c >= MIN_MEASURED), reverse=True)[:8]
    return {"written": len(small), "measured": checked, "overall_share": round(100 * overall, 2),
            "expected_boards": round(expected), "best_industries": [(i, round(100 * s, 1), c) for s, c, i in top]}


def run(argv: list[str]) -> int:
    stats = build()
    print(f"  written          {stats['written']:>10,} names -> {OUTPUT}")
    print(f"  measured on      {stats['measured']:>10,} companies of 11-50 ({stats['overall_share']}% had a board)")
    print(f"  likely to find   {stats['expected_boards']:>10,} boards")
    for industry, share, count in stats["best_industries"]:
        print(f"    {share:>5}%  {industry}  ({count:,} measured)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
