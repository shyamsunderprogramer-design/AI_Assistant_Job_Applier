"""Company discovery: company NAME -> ATS slug.

There is no public directory of every company on Greenhouse/Lever (see
README.md §C3), so the inventory is built two ways:

  1. Slug probing — derive candidate slugs from a name and ask each ATS API
     whether that board exists. Feed it any name list (Fortune 1000, a startup
     list, whatever) and it keeps the ones that are actually on these ATSs.
  2. CSV ingest — import a pre-built inventory with name/slug/source columns.

Both write into the `companies` table, so discovery runs once and every later
scrape reuses the result.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from db.models import Company
from db.session import get_session
from scraper.base import PortalScraper

log = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass
class DiscoveryResult:
    name: str
    slug: str | None
    source: str | None
    found: bool


def candidate_slugs(name: str, strip_suffixes: list[str] | None = None) -> list[str]:
    """Derive plausible ATS slugs from a company name, best guess first.

    Greenhouse/Lever slugs are usually the lowercased name with punctuation and
    corporate suffixes removed, e.g. "Ramp Financial, Inc." -> "ramp".
    """
    suffixes = {s.lower().strip(" .") for s in (strip_suffixes or [])}
    cleaned = name.lower().strip()
    cleaned = cleaned.replace("&", " and ")
    words = [w for w in _NON_ALNUM.split(cleaned) if w]

    while words and words[-1] in suffixes:
        words.pop()
    while words and words[0] in suffixes:
        words.pop(0)

    if not words:
        return []

    joined = "".join(words)
    hyphenated = "-".join(words)

    candidates = [joined]
    if hyphenated != joined:
        candidates.append(hyphenated)
    if len(words) > 1:
        candidates.append(words[0])  # "Palo Alto Networks" -> "paloalto..." then "palo"

    seen: set[str] = set()
    return [c for c in candidates if c and not (c in seen or seen.add(c))]


class CompanyDiscoverer:
    def __init__(self, scrapers: dict[str, PortalScraper], strip_suffixes: list[str] | None = None):
        self.scrapers = scrapers
        self.strip_suffixes = strip_suffixes or []

    def probe(self, name: str, sources: list[str] | None = None) -> DiscoveryResult:
        """Probe each ATS for this company. First hit wins."""
        for source in sources or list(self.scrapers):
            scraper = self.scrapers.get(source)
            if scraper is None:
                continue
            for slug in candidate_slugs(name, self.strip_suffixes):
                try:
                    exists = scraper.board_exists(slug)
                except Exception as exc:  # a probe failure is not fatal
                    log.debug("Probe error %s/%s: %s", source, slug, exc)
                    continue
                if exists:
                    log.info("Found %s on %s (slug: %s)", name, source, slug)
                    return DiscoveryResult(name=name, slug=slug, source=source, found=True)
        log.debug("No Greenhouse/Lever board found for %s", name)
        return DiscoveryResult(name=name, slug=None, source=None, found=False)

    def discover_from_names(self, names: list[str], sources: list[str] | None = None) -> list[DiscoveryResult]:
        results: list[DiscoveryResult] = []
        for name in names:
            name = name.strip()
            if not name:
                continue
            result = self.probe(name, sources)
            results.append(result)
            if result.found:
                self._save(result.name, result.slug, result.source, origin="discovery")
        return results

    def _save(self, name: str, slug: str, source: str, origin: str) -> None:
        board_url = self.scrapers[source].board_url(slug) if source in self.scrapers else None
        upsert_company(name=name, slug=slug, source=source, origin=origin, board_url=board_url)


def upsert_company(
    name: str, slug: str, source: str, origin: str = "config", board_url: str | None = None
) -> None:
    """Insert or refresh one company row. Idempotent on (source, slug)."""
    with get_session() as session:
        existing = (
            session.query(Company).filter_by(source=source, slug=slug).one_or_none()
        )
        if existing is None:
            session.add(
                Company(
                    name=name, slug=slug, source=source, origin=origin, board_url=board_url
                )
            )
        else:
            existing.name = name or existing.name
            existing.board_url = board_url or existing.board_url


def load_names_from_file(path: Path | str) -> list[str]:
    """Read company names from a .txt (one per line) or .csv (first column,
    or a 'name'/'company' column if present)."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        if not rows:
            return []
        header = [h.strip().lower() for h in rows[0]]
        name_idx = next(
            (i for i, h in enumerate(header) if h in ("name", "company", "company_name")), None
        )
        if name_idx is None:
            return [r[0].strip() for r in rows if r and r[0].strip()]
        return [r[name_idx].strip() for r in rows[1:] if len(r) > name_idx and r[name_idx].strip()]

    with open(path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip() and not line.startswith("#")]


def import_inventory_csv(path: Path | str) -> int:
    """Import a pre-built ATS inventory CSV with name, slug, source columns."""
    path = Path(path)
    imported = 0
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            keys = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            slug = keys.get("slug") or keys.get("token") or keys.get("board_token")
            source = (keys.get("source") or keys.get("ats") or "").lower()
            name = keys.get("name") or keys.get("company") or slug
            if not slug or source not in ("greenhouse", "lever"):
                continue
            upsert_company(name=name, slug=slug, source=source, origin="csv")
            imported += 1
    return imported
