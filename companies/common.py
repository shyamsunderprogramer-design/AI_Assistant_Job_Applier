"""Shared fetch/parse helpers for company importers."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import requests

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "companies_build" / "raw"
UA = {"User-Agent": "companies-db-builder/1.0 (personal research; one-off import)"}


def fetch(url: str, filename: str, *, binary: bool = False) -> Path:
    """Download once to raw/, reuse on re-run (registries rate-limit repeat hits)."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    dest = RAW_DIR / filename
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  using cached {dest.name} ({dest.stat().st_size:,} bytes)")
        return dest
    print(f"  GET {url}")
    r = requests.get(url, headers=UA, timeout=120)
    r.raise_for_status()
    dest.write_bytes(r.content)
    print(f"  saved {dest.name} ({len(r.content):,} bytes)")
    return dest


def read_csv_rows(path: Path) -> list[dict]:
    """Tolerant CSV/TSV reader — registries are inconsistent about encoding."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"cannot decode {path}")
    sample = text[:4096]
    dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|") if sample else csv.excel
    return list(csv.DictReader(io.StringIO(text), dialect=dialect))


def norm_key(row: dict, *names: str) -> str | None:
    """Case-insensitive column lookup across registry header variants."""
    lowered = {k.strip().lower(): v for k, v in row.items() if k}
    for n in names:
        v = lowered.get(n.strip().lower())
        if v and v.strip():
            return v.strip()
    return None


def find_gov_uk_asset(page_url: str, want: str = ".csv") -> str:
    """gov.uk publications link attachments from the HTML page; find the latest."""
    r = requests.get(page_url, headers=UA, timeout=60)
    r.raise_for_status()
    import re
    links = re.findall(r'https://assets\.publishing\.service\.gov\.uk/[^"\s<>]+', r.text)
    for link in links:
        if link.lower().endswith(want):
            return link
    raise RuntimeError(f"no {want} attachment found on {page_url} — pass the file manually")
