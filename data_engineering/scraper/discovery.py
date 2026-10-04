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
from concurrent.futures import ThreadPoolExecutor
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from data_engineering.db.models import Company, ProbeLog, utcnow
from data_engineering.db.session import get_session
from data_engineering.scraper.base import PortalScraper

log = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass
class DiscoveryResult:
    name: str
    slug: str | None
    source: str | None
    found: bool
    # True only when this run's own probe found the board, not the cache. A
    # "Known board" answered from cache was found on some earlier pass.
    new: bool = False


def candidate_slugs(
    name: str, strip_suffixes: list[str] | None = None, domain: str | None = None
) -> list[str]:
    """Derive plausible ATS slugs, best guess first.

    Greenhouse/Lever/Ashby slugs are usually the lowercased name with
    punctuation and corporate suffixes removed, e.g. "Ramp Financial, Inc." ->
    "ramp".

    A DOMAIN, when known, is the better guess and goes first: a slug matches the
    domain root far more often than the trading name. "Custom Computer
    Specialists" is at customtech.com, and no amount of name-mangling finds
    that.
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

    candidates = []
    from data_engineering.scraper.company_import import domain_root

    root = domain_root(domain)
    if root:
        candidates.append(root)

    candidates.append(joined)
    if hyphenated != joined:
        candidates.append(hyphenated)
    if len(words) > 1:
        candidates.append(words[0])  # "Palo Alto Networks" -> "paloalto..." then "palo"

    seen: set[str] = set()
    return [c for c in candidates if c and not (c in seen or seen.add(c))]


def load_probe_cache() -> dict[tuple[str, str], bool]:
    """Every (source, slug) already probed, mapped to whether it was a hit."""
    with get_session() as session:
        return {(row.source, row.slug): row.found for row in session.query(ProbeLog).all()}


def record_probe(source: str, slug: str, found: bool, company_name: str | None = None) -> None:
    """Remember a probe outcome so it is never re-sent."""
    try:
        _record_probe(source, slug, found, company_name)
    except IntegrityError:
        # Names are probed side by side, and "Acme" and "Acme Inc" make the
        # same guess: the other one recorded it a moment ago.
        pass


def _record_probe(source: str, slug: str, found: bool, company_name: str | None) -> None:
    with get_session() as session:
        existing = session.query(ProbeLog).filter_by(source=source, slug=slug).one_or_none()
        if existing is None:
            session.add(ProbeLog(source=source, slug=slug, found=found, company_name=company_name))
        else:
            existing.found = found
            existing.probed_at = utcnow()
            existing.company_name = company_name or existing.company_name


@dataclass
class DiscoveryProgress:
    """Counters for one discovery run."""

    names: int = 0
    found: int = 0
    probes_sent: int = 0
    probes_skipped: int = 0  # answered from cache, never sent

    def summary(self) -> str:
        saved = f", {self.probes_skipped} from cache" if self.probes_skipped else ""
        return (
            f"{self.found} found from {self.names} names; "
            f"{self.probes_sent} probes sent{saved}"
        )


class CompanyDiscoverer:
    def __init__(self, scrapers: dict[str, PortalScraper], strip_suffixes: list[str] | None = None,
                 single_variant: set[str] | None = None, name_workers: int = 1):
        self.scrapers = scrapers
        self.name_workers = name_workers
        self.strip_suffixes = strip_suffixes or []
        # Systems probed with only the first name variant. Where every company
        # shares ONE host (Greenhouse, Lever, Ashby, Workable, Jobvite) each
        # extra guess waits out that host's delay; measured on 26 Sep 2026 the
        # second variant found 90 of 3,465 boards (2.6%), so it is not worth
        # doubling the run. Per-company hosts (bunq.recruitee.com) cost no
        # waiting and keep every variant.
        self.single_variant = set(single_variant or ())
        self.progress = DiscoveryProgress()
        self._cache: dict[tuple[str, str], bool] = {}

    def plan(
        self,
        name: str,
        sources: list[str] | None = None,
        domain: str | None = None,
        max_slugs: int = 0,
    ) -> list[tuple[str, str]]:
        """The (source, slug) pairs this name would probe. Used by --dry-run.

        `max_slugs` caps the guesses per name. Across a large list this is the
        difference between a run that finishes and one that does not: the first
        candidate (the domain root, when known) carries most of the hit rate,
        and each extra guess costs a request against a live board.
        """
        slugs = candidate_slugs(name, self.strip_suffixes, domain=domain)
        if max_slugs:
            slugs = slugs[:max_slugs]
        return [
            (source, slug)
            for source in (sources or list(self.scrapers))
            if source in self.scrapers
            for slug in (slugs[:1] if source in self.single_variant else slugs)
        ]

    def probe(
        self,
        name: str,
        sources: list[str] | None = None,
        domain: str | None = None,
        max_slugs: int = 0,
    ) -> DiscoveryResult:
        """Probe each ATS for this company. First hit wins.

        A (source, slug) already in the probe cache is never re-requested: a
        dead slug stays dead, and re-asking is both slow and impolite.
        """
        pairs = self.plan(name, sources, domain=domain, max_slugs=max_slugs)

        # Answer from the cache first, and without sending anything. A hit here
        # settles the name for free.
        live: list[tuple[str, str]] = []
        for source, slug in pairs:
            cached = self._cache.get((source, slug))
            if cached is None:
                live.append((source, slug))
                continue
            self.progress.probes_skipped += 1
            if cached:
                log.info("Known board: %s on %s (slug: %s)", name, source, slug)
                return DiscoveryResult(name=name, slug=slug, source=source, found=True)

        if not live:
            return DiscoveryResult(name=name, slug=None, source=None, found=False)

        # The remaining probes go to Greenhouse, Lever and Ashby -- three
        # different hosts, which were being asked one after another. Each
        # waited out the others' 1.5s delay for no reason, so a single name
        # cost four and a half seconds and the run projected to 43 days.
        #
        # Asking them at once costs almost nothing in extra requests: the loop
        # only ever exited early on a hit, and the hit rate is about 2%, so 98%
        # of names were sending all three anyway. Per-host politeness is
        # untouched -- PoliteClient still paces each host separately.
        def ask(pair):
            source, slug = pair
            try:
                return pair, self.scrapers[source].board_exists(slug)
            except Exception as exc:          # a probe failure is not fatal
                log.debug("Probe error %s/%s: %s", source, slug, exc)
                return pair, None

        found_pair = None
        # One worker per pending probe: every source is a different host with
        # its own pacing, so running them side by side adds no load to any
        # one of them — it only stops seven hosts queueing behind four slots.
        with ThreadPoolExecutor(max_workers=min(len(live), 8)) as pool:
            for (source, slug), exists in pool.map(ask, live):
                if exists is None:
                    continue
                self.progress.probes_sent += 1
                # Persist immediately rather than at the end: a run interrupted
                # after 4,000 probes must not throw away what those cost.
                record_probe(source, slug, exists, company_name=name if exists else None)
                self._cache[(source, slug)] = exists
                # `plan` returns candidates best-first, and pool.map preserves
                # that order, so the first hit here is the same one the
                # sequential loop would have returned.
                if exists and found_pair is None:
                    found_pair = (source, slug)

        if found_pair:
            source, slug = found_pair
            log.info("Found %s on %s (slug: %s)", name, source, slug)
            return DiscoveryResult(name=name, slug=slug, source=source, found=True, new=True)

        log.debug("No board found for %s", name)
        return DiscoveryResult(name=name, slug=None, source=None, found=False)

    def discover_from_names(
        self,
        names: list[str],
        sources: list[str] | None = None,
        on_progress=None,
        max_slugs: int = 0,
    ) -> list[DiscoveryResult]:
        """Probe every name. Safe to interrupt — findings persist as they happen.

        A name may be "Company" or "Company,domain.com"; the domain becomes the
        first slug guess.
        """
        # Loaded once per discoverer: every probe it sends is added as it goes.
        # Reloading all of probe_log (850,000 rows, four seconds) before each
        # batch of forty names was pure waste.
        if not self._cache:
            self._cache = load_probe_cache()
        self.progress = DiscoveryProgress()
        results: list[DiscoveryResult] = []

        def one(raw):
            name, _, domain = str(raw).partition(",")
            name = name.strip()
            if not name:
                return None
            return self.probe(name, sources, domain=domain.strip() or None, max_slugs=max_slugs)

        # Several names at once. Each host is still paced on its own by the
        # client, so this asks no host any faster; it stops a name that waits
        # on Greenhouse from holding up the next one's BambooHR probe. One at
        # a time, the run spent most of its life waiting on the slowest host.
        # One worker runs in this thread, as it always did: an in-memory
        # database (the tests') exists only for the thread that made it.
        if self.name_workers <= 1:
            done = map(one, names)
            pool = None
        else:
            pool = ThreadPoolExecutor(max_workers=self.name_workers)
            done = pool.map(one, names)
        try:
            for result in done:
                if result is None:
                    continue
                self.progress.names += 1
                results.append(result)
                if result.found:
                    self.progress.found += 1
                    self._save(result.name, result.slug, result.source, origin="discovery")
                if on_progress is not None:
                    on_progress(self.progress, result)
        finally:
            if pool is not None:
                pool.shutdown(wait=True)
        return results

    def _save(self, name: str, slug: str, source: str, origin: str) -> None:
        board_url = self.scrapers[source].board_url(slug) if source in self.scrapers else None
        upsert_company(name=name, slug=slug, source=source, origin=origin, board_url=board_url)


def upsert_company(
    name: str, slug: str, source: str, origin: str = "config", board_url: str | None = None
) -> None:
    """Insert or refresh one company row. Idempotent on (source, slug)."""
    try:
        _upsert_company(name, slug, source, origin, board_url)
    except IntegrityError:
        pass                    # saved a moment ago by the name probed beside it


def _upsert_company(name: str, slug: str, source: str, origin: str, board_url: str | None) -> None:
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
        names = []
        for line in fh:
            # A "#" starts a comment anywhere on the line, not just at column
            # one: scan-mail writes "Acme    # seen 284x", and treating that
            # annotation as part of the name probes "acmeseen284x" instead.
            name = line.split("#", 1)[0].strip()
            if name:
                names.append(name)
        return names


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
            if not slug or source not in ("greenhouse", "lever", "ashby"):
                continue
            upsert_company(name=name, slug=slug, source=source, origin="csv")
            imported += 1
    return imported
