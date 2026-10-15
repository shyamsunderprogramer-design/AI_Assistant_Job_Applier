"""Layer 3c: every company's careers page, and the hiring system behind it.

`enrich_careers.py` asks whether `/careers` answers 200. That finds a page, but
not whose page, and not the board behind it -- and the board is what this tool
can read. This walks from what we know about each company to its careers page
and on to the board:

  1. **Our own postings.** A posting from a watched board names the board. An
     aggregator's link is followed, politely, to wherever it lands: the
     employer's board, the employer's site, or a wall. Most aggregator links
     end at a wall (robots.txt or a bot challenge); that is recorded, not
     argued with.
  2. **The company's domain** -- from `companies.website`, the 7.2-million-row
     dump matched by name, or the names list itself. The homepage is read for
     its careers link ("Careers", "Join us", jobs.acme.com); failing that the
     usual addresses are tried (/careers, /jobs, careers.<domain> ...).
  3. **The careers page** is read for the board it links, embeds or redirects
     to. A redirect into a hiring system is never followed into it: the URL
     already says whose board it is, and the running board search is the one
     that talks to those hosts.

Polite by construction: robots.txt is obeyed, each host is paced on its own
(the shared PoliteClient settings), a host that answers 429 is left alone, and
a host that does not resolve is not asked again. No CAPTCHAs, no logins, no
search engines, no keyed APIs. Each company is answered once; the answer is
saved the moment it arrives, so a stopped run -- a closed lid, a Stop -- picks
up where it left off.

    python main.py careers --list postings              # companies with postings
    python main.py careers --list worthwhile --limit 500
    python main.py careers --status | --stop | --export | --report
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from data_engineering.scraper.http_client import PoliteClient

from . import db as cdb
from .careers_detect import (
    AGGREGATOR_DOMAINS, CAREER_WORD, DomainCandidate, Hit, bot_wall, careers_signal,
    careers_url_from_posting, choose_board, clean_domain, clean_url, detect_html, detect_url,
    domain_root, extract_links, fingerprints, hiring_org_urls, host_of, is_parked, is_soft_404,
    meta_refresh, name_key, normalize_name, page_title, pick_domain, rank_careers_links,
    registrable_domain, script_redirect, slug_matches_name, urls_in,
)

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "careers"
CSV_PATH = OUT_DIR / "careers_sites.csv"
NEW_BOARDS_CSV = OUT_DIR / "new_boards.csv"
STATE = OUT_DIR / "state.json"
PROGRESS = OUT_DIR / "progress.json"
RUN_LOG = OUT_DIR / "run.log"
PIDFILE = OUT_DIR / "careers_finder.pid"
STOPFILE = OUT_DIR / "careers_finder.stop"
NAME_CACHE = OUT_DIR / "name_domains.json"
JOBS_DB = ROOT / "data" / "jobs.db"
BOARDS_CSV = ROOT / "backend" / "config" / "boards.csv"
DUMP = ROOT / "data_engineering" / "companies" / "companies_sorted.csv"
NAME_LISTS = {
    "worthwhile": ROOT / "data" / "refined_names_worthwhile.txt",
    "small": ROOT / "data" / "refined_names_small.txt",
}
LISTS = ("postings", "watched", "registry", "worthwhile", "small")

SCHEMA = """
CREATE TABLE IF NOT EXISTS careers_sites (
    list          TEXT NOT NULL,     -- postings | watched | registry | worthwhile | small
    item_key      TEXT NOT NULL,     -- the company within its list: name key, board, id or domain
    name          TEXT NOT NULL,
    domain        TEXT,              -- the company's own registrable domain, when known
    domain_source TEXT,              -- where that domain came from
    company_id    INTEGER,           -- companies.id, for registry rows
    careers_url   TEXT,              -- the careers page (own site, or the board itself)
    ats           TEXT,              -- hiring system behind it, NULL when not recognised
    ats_slug      TEXT,              -- the board's name on that system
    board_url     TEXT,
    method        TEXT,              -- how it was found
    evidence_url  TEXT,              -- where the system was seen (tracking parameters removed)
    status        TEXT NOT NULL,     -- found | no-careers-page | unreachable | blocked | robots |
                                     -- parked | no-domain | error
    detail        TEXT,
    checked_at    TEXT NOT NULL,
    PRIMARY KEY (list, item_key)
);
CREATE INDEX IF NOT EXISTS idx_careers_sites_domain ON careers_sites(domain);
"""

# Sources that are hiring systems the scraper already reads: their postings
# name the board outright. Everything else is an aggregator or an email alert.
from data_engineering.scraper.runner import SCRAPER_TYPES  # noqa: E402

WATCHED_SYSTEMS = frozenset(SCRAPER_TYPES)

# Answers that may be retried with --retry: the network's fault, not the site's.
RETRYABLE = ("error",)
FINAL_STATUSES = ("found", "no-careers-page", "parked", "robots", "blocked", "unreachable")

HOME_BYTES = 1_500_000        # footers hold the careers link: read the whole page
PAGE_BYTES = 1_000_000
ITEM_SECONDS = 150            # a company gets this long, all requests included
CONNECT_TIMEOUT = 6
READ_TIMEOUT = 12
BODY_SECONDS = 20


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

@dataclass
class Page:
    """What one URL turned out to be, after its redirects."""

    url: str
    final_url: str = ""
    status: int = 0
    html: str = ""
    error: str | None = None          # dns | connect | timeout | ssl | robots | rate-limited | ...
    blocked: str | None = None        # cloudflare | akamai | forbidden | ...
    stopped_at_ats: Hit | None = None # a redirect into a hiring system, not followed into it

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.html) and not self.blocked


class HostUnreachable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _classify(exc: BaseException) -> str:
    import requests

    text = repr(exc)
    if isinstance(exc, requests.exceptions.SSLError):
        return "ssl"
    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return "timeout"
    if any(s in text for s in ("NameResolution", "nodename nor servname", "Name or service not known",
                                "getaddrinfo", "No address associated", "Temporary failure in name")):
        return "dns"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "redirects"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connect"
    if isinstance(exc, (UnicodeError, ValueError, LookupError, requests.exceptions.InvalidURL)):
        return "bad-url"
    return "error"


def make_client(cfg):
    """The project's PoliteClient, with the timeouts a sweep of company sites needs."""
    from data_engineering.scraper.http_client import HttpSettings

    settings = HttpSettings.from_config(cfg)
    settings = replace(settings, timeout_seconds=READ_TIMEOUT)
    return CareersClient(settings)


_UNSEEN = object()


class CareersClient(PoliteClient):
    """PoliteClient, reading pages the way a careers sweep needs them read.

    The same robots.txt rules, the same per-host pacing and the same 429
    cool-off as every scraper here (it is the shared class). What differs is
    the failure handling: a scraper retries a board it knows exists, but a
    third of a company list does not resolve at all, and retrying a dead host
    four times with backoff turned a census into a crawl. Here a host that
    does not answer is remembered as dead for the run and never asked again.
    Redirects are followed one hop at a time, so a hop into a hiring system
    can be read from its URL instead of fetched.
    """

    def __init__(self, settings):
        super().__init__(settings)
        from requests.adapters import HTTPAdapter

        adapter = HTTPAdapter(pool_connections=128, pool_maxsize=4, max_retries=0)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        self._session.headers.update({
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        })
        self._dead: dict[str, str] = {}
        self.local = threading.local()

    # robots.txt: same reading as PoliteClient, but a host that cannot be
    # reached fails here, once, instead of again on the page itself.
    def _robots_for(self, host_root: str):
        import urllib.robotparser

        import requests

        # get, not `in` then []: another worker's request may clear the cache in between (_forget_old).
        cached = self._robots.get(host_root, _UNSEEN)
        if cached is not _UNSEEN:
            return cached
        if host_root in self._dead:
            raise HostUnreachable(self._dead[host_root])
        parser = urllib.robotparser.RobotFileParser()
        try:
            resp = self._session.get(f"{host_root}/robots.txt", timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                                     stream=True, allow_redirects=True)
            try:
                status = resp.status_code
                body = self._read(resp, 500_000) if status == 200 else ""
            finally:
                resp.close()
        except (requests.ConnectionError, requests.Timeout) as exc:
            reason = _classify(exc)
            if reason in ("dns", "connect", "timeout"):
                self._dead[host_root] = reason
                raise HostUnreachable(reason) from None
            self._robots[host_root] = None
            return None
        except (requests.RequestException, UnicodeError, ValueError, LookupError):
            self._robots[host_root] = None
            return None
        if status == 200:
            parser.parse(body.splitlines())
        elif 400 <= status < 500:
            parser.parse([])                      # RFC 9309: unavailable means allowed
        else:
            parser.parse(["User-agent: *", "Disallow: /"])   # a server in trouble is not permission
        self._robots[host_root] = parser
        return parser

    def _forget_old(self) -> None:
        super()._forget_old()
        # A sweep meets each host once; without this the pacing tables grow by
        # one entry per company for the whole multi-day run.
        if len(self._last_request_at) > 20_000:
            cutoff = time.monotonic() - 120
            with self._locks_guard:
                for host, last in list(self._last_request_at.items()):
                    lock = self._host_locks.get(host)
                    if last < cutoff and (lock is None or not lock.locked()):
                        self._last_request_at.pop(host, None)
                        self._host_locks.pop(host, None)
        if len(self._dead) > 200_000:
            self._dead.clear()
        if len(self._cooling_until) > 20_000:
            now = time.monotonic()
            for host, until in list(self._cooling_until.items()):
                if until < now:
                    self._cooling_until.pop(host, None)
                    self._refusals.pop(host, None)

    @staticmethod
    def _read(resp, limit: int) -> str:
        started = time.monotonic()
        body = bytearray()
        for chunk in resp.iter_content(chunk_size=65536):
            body.extend(chunk)
            if len(body) >= limit or time.monotonic() - started > BODY_SECONDS:
                break
        raw = bytes(body[:limit])
        enc = None
        m = re.search(r"charset=([\w-]+)", resp.headers.get("Content-Type", ""), re.I)
        if m:
            enc = m.group(1)
        else:
            m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
            if m:
                enc = m.group(1).decode("ascii", "ignore")
        try:
            return raw.decode(enc or "utf-8", errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    def _count_error(self) -> None:
        self.local.net_errors = getattr(self.local, "net_errors", 0) + 1

    def fetch(self, url: str, *, max_bytes: int = PAGE_BYTES, max_hops: int = 6) -> Page:
        """GET one page, redirects included, without ever entering a hiring system."""
        import requests

        self._forget_old()
        page = Page(url=url, final_url=url)
        current = url
        retried = False
        hop = 0
        while hop <= max_hops:
            # A hiring system's URL already says whose board it is. The board
            # search is the one that talks to those hosts; this never does.
            hit = detect_url(current)
            if hit:
                page.final_url, page.stopped_at_ats = current, hit
                return page
            try:
                parts = urlparse(current)
            except ValueError:
                page.error = "bad-url"
                return page
            if parts.scheme not in ("http", "https") or not parts.hostname:
                page.error = "bad-url"
                return page
            netloc = parts.netloc
            origin = f"{parts.scheme}://{netloc}"
            if origin in self._dead:
                page.error = self._dead[origin]
                return page
            if self.cooling_left(netloc) > 0:
                page.error = "rate-limited"
                return page
            try:
                if not self.allowed(current):
                    page.final_url, page.error = current, "robots"
                    return page
            except HostUnreachable as exc:
                self._count_error()
                page.final_url, page.error = current, exc.reason
                return page
            except (UnicodeError, ValueError, LookupError):
                page.error = "bad-url"
                return page
            self._wait_turn(netloc)
            try:
                resp = self._session.get(current, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                                         allow_redirects=False, stream=True)
            except requests.RequestException as exc:
                reason = _classify(exc)
                if reason in ("dns", "connect") or (reason == "timeout" and isinstance(
                        exc, requests.exceptions.ConnectTimeout)):
                    self._dead[origin] = reason
                if reason in ("dns", "connect", "timeout"):
                    self._count_error()
                page.final_url, page.error = current, reason
                return page
            except (UnicodeError, ValueError, LookupError):
                page.error = "bad-url"
                return page
            try:
                status = resp.status_code
                location = resp.headers.get("Location")
                headers = dict(resp.headers)
                if status in (301, 302, 303, 307, 308) and location:
                    try:
                        current = urljoin(current, location.strip())
                    except ValueError:
                        page.error = "bad-url"
                        return page
                    hop += 1
                    continue
                if status == 429:
                    n = self._refusals.get(netloc, 0) + 1
                    self._refusals[netloc] = n
                    cooldown = min(self.settings.rate_limit_cooldown_seconds * 2 ** (n - 1), 6 * 3600)
                    self._cooling_until[netloc] = time.monotonic() + cooldown
                    page.final_url, page.status, page.error = current, status, "rate-limited"
                    return page
                ctype = (resp.headers.get("Content-Type") or "").lower()
                body = ""
                if not ctype or any(t in ctype for t in ("html", "xml", "text/plain")):
                    try:
                        body = self._read(resp, max_bytes)
                    except (requests.RequestException, OSError):
                        body = ""
            finally:
                resp.close()

            wall = bot_wall(status, headers, body)
            if status >= 500 and not wall and not retried:
                retried = True
                time.sleep(3)
                continue                     # once: a server having a moment
            page.final_url, page.status, page.html = current, status, body
            if wall:
                page.blocked, page.error = wall, f"blocked:{wall}"
                return page
            if status == 200 and hop < max_hops:
                target = meta_refresh(body, current) or script_redirect(body, current)
                if target and target.rstrip("/") != current.rstrip("/") and target.startswith("http"):
                    current = target
                    hop += 1
                    continue
            self._refusals.pop(netloc, None)
            return page
        page.error = "redirects"
        return page


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    status: str
    careers_url: str | None = None
    hit: Hit | None = None
    method: str | None = None
    evidence: str | None = None
    domain: str | None = None
    domain_source: str | None = None
    detail: str | None = None
    net_errors: int = 0
    cut_short: bool = False          # a stop or the time limit ended it early


@dataclass
class Item:
    """One company in one list."""

    list: str
    key: str
    name: str
    domain: str | None = None
    domain_source: str | None = None
    company_id: int | None = None
    known: Outcome | None = None                       # settled without the network
    postings: list[tuple[str, str]] = field(default_factory=list)  # (source, application_url)
    text_urls: list[str] = field(default_factory=list)  # URLs written in posting texts
    careers_hint: str | None = None                    # a careers URL known in advance
    postings_count: int = 0


# Job-page hosts worth asking first: they answer, and their pages name the employer.
_SOURCE_PREFERENCE = ["dice-email", "jobicy", "findwork", "themuse", "remoteok", "weworkremotely",
                      "workingnomads", "hn-hiring", "himalayas", "adzuna", "indeed-email"]

_LISTING_TEXT = CAREER_WORD


class Finder:
    """Company -> careers page -> board. `fetch` is injected, so tests need no network."""

    def __init__(self, fetch, index: "DomainIndex | None" = None, *, deadline_s: float = ITEM_SECONDS,
                 stop: threading.Event | None = None):
        self.fetch = fetch
        self.index = index
        self.deadline_s = deadline_s
        self.stop = stop or threading.Event()
        self._strikes: Counter = Counter()          # aggregator host -> refusals in a row
        self._lock = threading.Lock()
        self._local = threading.local()

    # -- time ------------------------------------------------------------
    def _out_of_time(self) -> bool:
        return self.stop.is_set() or time.monotonic() > getattr(self._local, "deadline", float("inf"))

    def _get(self, url: str, **kw) -> Page:
        if self._out_of_time():
            self._local.cut = True
            return Page(url=url, final_url=url, error="deadline")
        return self.fetch(url, **kw)

    # -- one company -----------------------------------------------------
    def find(self, item: Item) -> Outcome:
        self._local.deadline = time.monotonic() + self.deadline_s
        self._local.cut = False
        self._local.home = None
        outcome = self._find(item)
        if self._local.cut and outcome.status != "found":
            # Unfinished, not answered: saved as an error so --retry asks again.
            outcome = replace(outcome, status="error", cut_short=True,
                              detail=f"ran out of time ({outcome.status})")
        return outcome

    def _find(self, item: Item) -> Outcome:
        if item.known is not None and item.known.status == "found":
            known = item.known
            if not known.domain and self.index is not None:
                domain, why = self.index.lookup(item.name)       # no request: just the lists
                if domain:
                    known = replace(known, domain=domain, domain_source=f"name match ({why})")
            return known
        notes: list[str] = []
        domain, domain_source, hint = item.domain, item.domain_source, item.careers_hint

        # 1. What the postings already say, without asking anyone.
        for source, url in item.postings:
            hit = detect_url(url)
            if hit and (hit.slug or hit.board_url):
                return Outcome("found", hit.board_url or hit.seen_at, hit, f"{source} posting links the board",
                               hit.seen_at, domain, domain_source)
            reg = registrable_domain(url)
            if reg and reg not in AGGREGATOR_DOMAINS and not domain:
                domain, domain_source = reg, f"{source} posting link"
                hint = hint or careers_url_from_posting(url)
        for url in item.text_urls:
            hit = detect_url(url)
            if hit and hit.slug and slug_matches_name(hit.slug, item.name):
                return Outcome("found", hit.board_url or hit.seen_at, hit, "posting text links the board",
                               hit.seen_at, domain, domain_source)
            reg = registrable_domain(url)
            if not domain and reg and reg not in AGGREGATOR_DOMAINS and \
                    slug_matches_name(domain_root(reg), item.name):
                domain, domain_source = reg, "posting text"

        # 2. Follow an aggregator link or two to wherever it lands.
        if not domain and item.postings:
            for source, url in self._ordered(item.postings)[:2]:
                outcome = self._follow_posting(item, source, url, notes)
                if outcome is None:
                    continue
                if outcome.status == "found":
                    return outcome
                domain, domain_source = outcome.domain, outcome.domain_source
                hint = hint or outcome.careers_url
                break

        # 3. The name, matched against the company lists.
        if not domain and self.index is not None:
            domain, why = self.index.lookup(item.name)
            if domain:
                domain_source = f"name match ({why})"
            else:
                notes.append(why)

        if not domain:
            if item.known is not None:
                return item.known
            return Outcome("no-domain", detail="; ".join(notes) or "no domain known")

        outcome = self.find_on_domain(domain, item.name, hint=hint)
        outcome.domain = outcome.domain or domain
        outcome.domain_source = domain_source
        if notes and outcome.status != "found":
            outcome.detail = "; ".join([*notes, outcome.detail or ""]).strip("; ")
        if item.known is not None and outcome.status != "found":
            return replace(item.known, domain=outcome.domain, domain_source=domain_source)
        return outcome

    @staticmethod
    def _ordered(postings: list[tuple[str, str]]) -> list[tuple[str, str]]:
        rank = {s: i for i, s in enumerate(_SOURCE_PREFERENCE)}
        seen, out = set(), []
        for source, url in sorted(postings, key=lambda p: rank.get(p[0], len(rank))):
            if source in seen:
                continue
            seen.add(source)
            out.append((source, url))
        return out

    def _follow_posting(self, item: Item, source: str, url: str, notes: list[str]) -> Outcome | None:
        """An aggregator's link, followed to the employer, its board, or a wall."""
        host = host_of(url)
        site = registrable_domain(host) or host
        with self._lock:
            if self._strikes[host] >= 3:
                notes.append(f"{site} turns us away")
                return None
        page = self._get(url)
        if page.error == "robots":
            notes.append(f"robots.txt disallows following {site} links")
            return None
        if page.blocked or page.status in (401, 403) or page.error == "rate-limited":
            with self._lock:
                self._strikes[host] += 1
            notes.append(f"{site} answered {page.status or page.error}"
                         + (f" ({page.blocked})" if page.blocked and page.blocked != "forbidden" else ""))
            return None
        if page.error and not page.stopped_at_ats:
            notes.append(f"{site} link failed ({page.error})")
            return None
        with self._lock:
            self._strikes.pop(host, None)
        if page.stopped_at_ats:
            hit = page.stopped_at_ats
            return Outcome("found", hit.board_url or hit.seen_at, hit, f"{source} link redirects to the board",
                           hit.seen_at)
        final_site = registrable_domain(page.final_url)
        if not page.ok:
            notes.append(f"{site} link answered {page.status}")
            return None
        if final_site and final_site not in AGGREGATOR_DOMAINS:
            # Landed on a company site. Taken only if it is plausibly this
            # company's: a staffing agency's page would be someone else's.
            hit = choose_board(detect_html(page.html), item.name, final_site)
            if hit:
                return Outcome("found", careers_url_from_posting(page.final_url) or hit.board_url,
                               hit, f"{source} link lands on the employer's job page", hit.seen_at,
                               final_site, f"{source} link")
            title = page_title(page.html).lower()
            words = [w for w in name_key(item.name).split() if len(w) >= 4]
            if slug_matches_name(domain_root(final_site), item.name) or any(w in title for w in words[:2]):
                return Outcome("lead", careers_url_from_posting(page.final_url), domain=final_site,
                               domain_source=f"{source} link")
            notes.append(f"{site} link lands on {final_site}, not obviously {item.name}")
            return None
        # Still on the aggregator: its job page may name the board or the employer.
        hit = choose_board(detect_html(page.html), item.name, None, strict=True)
        if hit:
            return Outcome("found", hit.board_url or hit.seen_at, hit, f"{source} job page links the board",
                           hit.seen_at)
        for org_url in hiring_org_urls(page.html):
            reg = registrable_domain(org_url)
            if reg and reg not in AGGREGATOR_DOMAINS:
                return Outcome("lead", None, domain=reg, domain_source=f"{source} job page")
        notes.append(f"{site} job page names no employer site")
        return None

    # -- one domain ------------------------------------------------------
    def find_on_domain(self, domain: str, name: str | None, hint: str | None = None) -> Outcome:
        if hint and detect_url(hint):
            hit = detect_url(hint)
            if hit.slug or hit.board_url:
                return Outcome("found", hit.board_url or hit.seen_at, hit, "careers URL already known",
                               hit.seen_at, domain)
        if hint:
            page = self._get(hint)
            found = self._assess(page, hint, name, registrable_domain(hint) or domain,
                                 "careers URL already known", strong=True)
            if found:
                return found

        home = self._get(f"https://{domain}/", max_bytes=HOME_BYTES)
        if home.error in ("dns", "connect") and not domain.startswith("www."):
            home = self._get(f"https://www.{domain}/", max_bytes=HOME_BYTES)
        if home.error in ("ssl", "connect", "timeout"):
            plain = self._get(f"http://{domain}/", max_bytes=HOME_BYTES)
            if not plain.error or plain.html:
                home = plain
        if home.stopped_at_ats:
            hit = home.stopped_at_ats
            return Outcome("found", hit.board_url or hit.seen_at, hit, "homepage redirects to the board",
                           hit.seen_at, domain)
        if home.error == "robots":
            return Outcome("robots", domain=domain, detail="robots.txt disallows the homepage")
        if home.blocked:
            return Outcome("blocked", domain=domain,
                           detail=f"homepage answered {home.status} ({home.blocked})")
        if home.error and not home.html:
            status = "unreachable" if home.error in ("dns", "connect", "timeout", "ssl", "redirects",
                                                      "bad-url") else "error"
            if home.error == "rate-limited":
                status = "blocked"
            return Outcome(status, domain=domain, detail=f"homepage: {home.error}")
        if home.ok and is_parked(home.html, home.final_url):
            return Outcome("parked", domain=domain, detail=f"parked at {host_of(home.final_url)}")
        if home.ok:
            self._local.home = (page_title(home.html), len(home.html))

        site = registrable_domain(home.final_url) or domain
        links = extract_links(home.html, home.final_url) if home.ok else []
        candidates = rank_careers_links(links, site)
        board_links = [c.hit for c in candidates if c.hit]
        home_board = choose_board(board_links, name, site) if board_links else None
        tried: set[str] = set()
        walled: tuple | None = None           # a plain "Careers" link whose page refuses us
        refused: set[str] = set()             # hosts that answered with a wall
        attempts = 0
        for cand in [c for c in candidates if not c.hit]:
            if attempts >= 2:
                break
            if host_of(cand.url) in refused:
                continue                       # the same wall would answer again
            tried.add(cand.url.rstrip("/"))
            attempts += 1
            page = self._get(cand.url)
            found = self._assess(page, cand.url, name, site, "homepage link", strong=cand.score >= 70,
                                 fallback=home_board)
            if found:
                return found
            if page.blocked or page.status in (401, 403, 429) or page.error == "rate-limited":
                refused.add(host_of(cand.url))
                if walled is None and cand.score >= 70:
                    walled = (cand, page)
        if home_board:
            return Outcome("found", home_board.board_url or home_board.seen_at, home_board,
                           "homepage links the board", home_board.seen_at, site)

        host = host_of(home.final_url) if home.ok else domain
        scheme = "https" if not (home.final_url or "").startswith("http://") else "http"
        probes = [f"{scheme}://{host}/careers", f"{scheme}://{host}/jobs", f"https://careers.{site}/",
                  f"https://jobs.{site}/"]
        if len(links) < 15:
            probes += [f"{scheme}://{host}/company/careers", f"{scheme}://{host}/about/careers"]
        for url in probes:
            if url.rstrip("/") in tried or host_of(url) in refused:
                continue
            page = self._get(url)
            found = self._assess(page, url, name, site, "common address", strong=False)
            if found:
                return found
            if page.error == "deadline":
                break

        if walled is not None:
            # The homepage says plainly where its careers page is; that page
            # just will not be read by a program. The address is still the answer.
            cand, page = walled
            why = page.blocked if page.blocked and page.blocked != "forbidden" else f"HTTP {page.status}"
            return Outcome("found", clean_url(cand.url), None, "homepage link (page refuses automated visits)",
                           clean_url(cand.url), site, detail=f"careers page answered {why}")

        if not home.ok:
            return Outcome("unreachable" if home.status >= 400 or home.error else "no-careers-page",
                           domain=site, detail=f"homepage answered {home.status or home.error}")
        why = "homepage names no careers page" if links else "homepage has no readable links"
        return Outcome("no-careers-page", domain=site, detail=f"{why}; common addresses absent")

    def _assess(self, page: Page, requested: str, name: str | None, site: str | None, method: str,
                strong: bool, fallback: Hit | None = None) -> Outcome | None:
        """Is this fetched page the careers page? If so, which board does it use?"""
        if page.stopped_at_ats:
            hit = page.stopped_at_ats
            own = registrable_domain(requested) == site and not detect_url(requested)
            return Outcome("found", clean_url(requested) if own else (hit.board_url or hit.seen_at), hit,
                           f"{method} redirects to the board", hit.seen_at, site)
        if not page.ok:
            return None
        final = page.final_url
        final_site = registrable_domain(final)
        parts = urlparse(final)
        path = (parts.path or "/").rstrip("/")
        requested_path = (urlparse(requested).path or "/").rstrip("/")
        careers_host = bool(CAREER_WORD.search((parts.hostname or "").split(".")[0]))
        if not path and requested_path and not careers_host:
            return None                          # bounced back to the homepage
        if final_site != site and not strong:
            return None                          # wandered off the company's site
        if is_soft_404(page.html):
            return None
        home = getattr(self._local, "home", None)
        if home and not strong and page_title(page.html) == home[0] and \
                abs(len(page.html) - home[1]) <= max(500, home[1] // 20):
            return None                          # every address answers with the homepage
        if careers_signal(page.html, final) < (2 if strong else 3):
            return None

        board = choose_board(detect_html(page.html, final), name, site)
        if board is None:
            board = self._second_hop(page, name, site)
        if board is None:
            prints = fingerprints(page.html)
            if prints:
                board = Hit(prints[0], None, None, clean_url(final))
        if board is None and fallback is not None:
            board = fallback
        return Outcome("found", clean_url(final), board, method,
                       board.seen_at if board else clean_url(final), site)

    def _second_hop(self, page: Page, name: str | None, site: str | None) -> Hit | None:
        """A careers page that only says "See open roles": follow that one link."""
        links = extract_links(page.html, page.final_url)
        frames = [link for link in links if link.kind == "iframe"]
        listing = [link for link in links if link.kind == "a" and link.text and len(link.text) <= 40
                   and _LISTING_TEXT.search(link.text)]
        for link in frames + listing:
            hit = detect_url(link.url)
            if hit:
                return hit
        current = page.final_url.rstrip("/")
        for link in frames[:1] + listing[:3]:
            url = link.url.rstrip("/")
            if url == current or registrable_domain(url) != site:
                continue
            nxt = self._get(link.url)
            if nxt.stopped_at_ats:
                return nxt.stopped_at_ats
            if nxt.ok:
                hit = choose_board(detect_html(nxt.html, nxt.final_url), name, site)
                if hit is None and detect_url(nxt.final_url):
                    hit = detect_url(nxt.final_url)
                if hit is None:
                    prints = fingerprints(nxt.html)
                    hit = Hit(prints[0], None, None, clean_url(nxt.final_url)) if prints else None
                return hit
            return None
        return None


# ---------------------------------------------------------------------------
# Names -> domains
# ---------------------------------------------------------------------------

class DomainIndex:
    """Company name -> domain, from companies.db and the 7.2M-row dump.

    The dump is a gigabyte, so it is read once per new set of names and the
    matches kept in data/careers/name_domains.json; a re-run reads the cache.
    """

    def __init__(self, dump: Path = DUMP, cache_path: Path = NAME_CACHE):
        self.dump = dump
        self.cache_path = cache_path
        self.cache: dict[str, list] = {}
        self.registry: dict[str, list] = defaultdict(list)

    def load_registry(self, db: sqlite3.Connection) -> None:
        for cid, name, website, country in db.execute(
                "SELECT id, name, website, country FROM companies WHERE website IS NOT NULL AND website != ''"):
            domain = registrable_domain(website)
            if not domain:
                continue
            for key in {normalize_name(name), name_key(name)} - {""}:
                self.registry[key].append((domain, country or "", cid))

    def prepare(self, names, say=print) -> None:
        keys = set()
        for name in names:
            keys |= {normalize_name(name), name_key(name)}
        keys.discard("")
        try:
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}
        missing = keys - self.cache.keys()
        if not missing or not self.dump.exists():
            return
        say(f"matching {len(missing):,} names against {self.dump.name} (about two minutes, once)")
        firsts = set()
        for key in missing:
            words = key.split()
            firsts.add(words[0])
            if words[0] == "the" and len(words) > 1:
                firsts.add(words[1])
        found: dict[str, list] = defaultdict(list)
        import re

        token = re.compile(r"[a-z0-9]+")
        csv.field_size_limit(10 ** 7)
        with open(self.dump, newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                raw = (row.get("name") or "")
                head = token.findall(raw[:80].lower())
                if not head or (head[0] not in firsts and (len(head) < 2 or head[1] not in firsts)):
                    continue
                domain = clean_domain(row.get("domain"))
                if not domain:
                    continue
                try:
                    employees = int(float(row.get("total employee estimate") or 0))
                except ValueError:
                    employees = 0
                entry = [domain, (row.get("country") or "").strip(), employees]
                for key in {normalize_name(raw), name_key(raw)}:
                    if key in missing and len(found[key]) < 60:
                        found[key].append(entry)
        for key in missing:
            self.cache[key] = found.get(key, [])
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache), encoding="utf-8")
        tmp.replace(self.cache_path)

    def lookup(self, name: str) -> tuple[str | None, str]:
        for key in (normalize_name(name), name_key(name)):
            dump = self.cache.get(key) or []
            reg = self.registry.get(key) or []
            if not dump and not reg:
                continue
            candidates = [DomainCandidate(d, c, e) for d, c, e in dump]
            candidates += [DomainCandidate(d, c, 0) for d, c, _ in reg]
            return pick_domain(name, candidates)
        return None, "name not in the company lists"


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

def _jobs_db() -> sqlite3.Connection:
    """data/jobs.db, read-only: the web app and the daily run own it."""
    return sqlite3.connect(f"file:{JOBS_DB}?mode=ro", uri=True, timeout=30)


def _board_url(system: str, slug: str) -> str | None:
    scraper = SCRAPER_TYPES.get(system)
    try:
        return scraper(None).board_url(slug) if scraper else None
    except Exception:
        return None


def postings_items() -> list[Item]:
    """Every company with a posting: aggregator- and alert-only companies first."""
    from data_engineering.scraper.company_import import is_junk

    groups: dict[str, dict] = {}
    with _jobs_db() as db:
        for company, source, slug, url, description in db.execute(
                "SELECT company, source, company_slug, application_url, description FROM jobs"):
            if not company or is_junk(company):
                continue
            key = name_key(company)
            if not key:
                continue
            g = groups.setdefault(key, {"names": Counter(), "boards": Counter(), "board_urls": {},
                                        "postings": [], "texts": [], "count": 0, "usajobs": None})
            g["names"][company.strip()] += 1
            g["count"] += 1
            if source in WATCHED_SYSTEMS and slug:
                g["boards"][(source, slug)] += 1
                g["board_urls"].setdefault((source, slug), url)
            elif source == "usajobs":
                g["usajobs"] = url
            elif len(g["postings"]) < 6 and url:
                g["postings"].append((source, url))
            if description and len(g["texts"]) < 20 and source not in WATCHED_SYSTEMS:
                for found in urls_in(description, limit=10):
                    if found not in g["texts"]:
                        g["texts"].append(found)

    unwatched, watched = [], []
    for key, g in groups.items():
        name = g["names"].most_common(1)[0][0]
        item = Item("postings", key, name, postings=g["postings"], text_urls=g["texts"][:20],
                    postings_count=g["count"])
        if g["boards"]:
            (system, slug), _ = g["boards"].most_common(1)[0]
            url = g["board_urls"][(system, slug)]
            hit = Hit(system, slug, _board_url(system, slug), clean_url(url))
            own = None if detect_url(url) else careers_url_from_posting(url)
            item.known = Outcome("found", own or hit.board_url, hit, f"posting on a watched {system} board",
                                 hit.seen_at, registrable_domain(url) if own else None,
                                 "posting link" if own else None)
            watched.append(item)
        elif g["usajobs"]:
            hit = Hit("usajobs", None, "https://www.usajobs.gov/", clean_url(g["usajobs"]))
            item.known = Outcome("found", "https://www.usajobs.gov/", hit, "federal posting on USAJOBS",
                                 hit.seen_at)
            watched.append(item)
        else:
            unwatched.append(item)
    unwatched.sort(key=lambda i: -i.postings_count)
    watched.sort(key=lambda i: -i.postings_count)
    return unwatched + watched


def load_boards() -> list[dict]:
    """backend/config/boards.csv, read only: the board search is still writing it."""
    try:
        with open(BOARDS_CSV, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def watched_items() -> list[Item]:
    items, seen = [], set()
    for row in load_boards():
        system, slug = (row.get("source") or "").strip(), (row.get("slug") or "").strip()
        if not system or not slug or (system, slug) in seen:
            continue
        seen.add((system, slug))
        board = (row.get("board_url") or "").strip() or _board_url(system, slug)
        hit = Hit(system, slug, board, board)
        known = Outcome("found", board, hit, "watched board (boards.csv)", board)
        items.append(Item("watched", f"{system}:{slug}", (row.get("name") or slug).strip(), known=known))
    return items


def registry_items(db: sqlite3.Connection) -> list[Item]:
    items = []
    for cid, name, website, careers in db.execute(
            "SELECT id, name, website, careers_url FROM companies WHERE website IS NOT NULL AND website != ''"
            " ORDER BY CASE tier WHEN 'fortune500' THEN 0 WHEN 'sponsor' THEN 1 ELSE 2 END, id"):
        domain = registrable_domain(website)
        if not domain:
            continue
        items.append(Item("registry", f"id:{cid}", name, domain=domain, domain_source="companies.website",
                          company_id=cid, careers_hint=careers or None))
    return items


def names_items(list_name: str) -> list[Item]:
    from data_engineering.scraper.discovery import load_names_from_file

    items, seen = [], set()
    for line in load_names_from_file(NAME_LISTS[list_name]):
        name, _, raw = line.partition(",")
        domain = clean_domain(raw)
        if not domain or domain in seen:
            continue
        seen.add(domain)
        items.append(Item(list_name, domain, name.strip() or domain, domain=domain,
                          domain_source=f"{NAME_LISTS[list_name].name}"))
    return items


def build_items(list_name: str, db: sqlite3.Connection) -> list[Item]:
    if list_name == "postings":
        return postings_items()
    if list_name == "watched":
        return watched_items()
    if list_name == "registry":
        return registry_items(db)
    if list_name in NAME_LISTS:
        return names_items(list_name)
    raise SystemExit(f"unknown list {list_name!r}: choose from {', '.join(LISTS)}")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def connect() -> sqlite3.Connection:
    db = cdb.connect()
    db.executescript(SCHEMA)
    return db


def to_row(item: Item, outcome: Outcome) -> tuple:
    hit = outcome.hit
    return (item.list, item.key, item.name, outcome.domain or item.domain,
            outcome.domain_source or item.domain_source, item.company_id, outcome.careers_url,
            hit.system if hit else None, hit.slug if hit else None, hit.board_url if hit else None,
            outcome.method, outcome.evidence, outcome.status, (outcome.detail or "")[:400] or None, utcnow())


def save(db: sqlite3.Connection, rows: list[tuple], fill_registry: bool = True) -> None:
    db.executemany("INSERT OR REPLACE INTO careers_sites VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    if fill_registry:
        # Registry companies get their careers_url filled, never overwritten.
        for row in rows:
            if row[0] == "registry" and row[5] and row[12] == "found" and row[6]:
                db.execute("UPDATE companies SET careers_url = ?, updated_at = ? "
                           "WHERE id = ? AND careers_url IS NULL", (row[6], utcnow(), row[5]))
    db.commit()


def watched_boards() -> set[tuple[str, str]]:
    return {((r.get("source") or "").strip(), (r.get("slug") or "").strip().lower()) for r in load_boards()}


CSV_FIELDS = ("list", "name", "domain", "careers_url", "hiring_system", "board_slug", "board_url",
              "already_watched", "found_by", "status", "detail", "checked_at", "domain_source")


def export(db: sqlite3.Connection) -> tuple[int, int]:
    """data/careers/careers_sites.csv, plus new_boards.csv: boards our scrapers read but nobody watches."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    watched = watched_boards()
    rows = db.execute("SELECT list, name, domain, careers_url, ats, ats_slug, board_url, method, status,"
                      " detail, checked_at, domain_source FROM careers_sites ORDER BY list, name")
    new: dict[tuple[str, str], tuple] = {}
    count = 0
    tmp = CSV_PATH.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as handle:
        out = csv.writer(handle)
        out.writerow(CSV_FIELDS)
        for (lst, name, domain, careers, ats, slug, board, method, status, detail, checked,
             source) in rows:
            is_watched = bool(ats and slug and (ats, slug.lower()) in watched)
            out.writerow((lst, name, domain or "", careers or "", ats or "", slug or "", board or "",
                          "yes" if is_watched else ("no" if ats and slug else ""), method or "", status,
                          detail or "", checked, source or ""))
            count += 1
            if ats in WATCHED_SYSTEMS and slug and not is_watched and status == "found":
                new.setdefault((ats, slug.lower()), (ats, slug, name, board or _board_url(ats, slug) or ""))
    tmp.replace(CSV_PATH)
    tmp = NEW_BOARDS_CSV.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as handle:
        out = csv.writer(handle)
        out.writerow(("source", "slug", "name", "board_url", "origin"))
        for ats, slug, name, board in sorted(new.values()):
            out.writerow((ats, slug, name, board, "careers-finder"))
    tmp.replace(NEW_BOARDS_CSV)
    return count, len(new)


_ENGINE_READY = False


def watch_new_boards(cfg) -> int:
    """The boards just written to new_boards.csv, added to the daily fetch: the number new to it.

    The same path the board list takes in from a file (data_engineering/boards.import_), so a found
    board is fetched from the next morning on, as the board search does with what it finds. A board
    the fetch has retired after failing is not revived -- the upsert never sets one active again.
    """
    global _ENGINE_READY
    from data_engineering import boards
    from data_engineering.db.models import Company
    from data_engineering.db.session import get_session, init_engine
    if not NEW_BOARDS_CSV.exists():
        return 0
    if not _ENGINE_READY:
        init_engine(cfg.database_url)
        _ENGINE_READY = True
    with get_session() as session:
        before = session.query(Company).count()
    boards.import_(NEW_BOARDS_CSV)
    with get_session() as session:
        added = session.query(Company).count() - before
    if added:
        # Written aside and moved into place: the board search writes this file too.
        tmp = BOARDS_CSV.with_suffix(".careers.tmp")
        boards.export(tmp)
        tmp.replace(BOARDS_CSV)
    return added


def summary(db: sqlite3.Connection, list_name: str | None = None) -> dict:
    where, params = ("WHERE list = ?", (list_name,)) if list_name else ("", ())
    statuses = dict(db.execute(f"SELECT status, count(*) FROM careers_sites {where} GROUP BY status", params))
    systems = dict(db.execute(f"SELECT ats, count(*) FROM careers_sites {where} "
                              f"{'AND' if where else 'WHERE'} ats IS NOT NULL AND status = 'found' "
                              f"GROUP BY ats ORDER BY 2 DESC", params))
    total = sum(statuses.values())
    return {"total": total, "statuses": statuses, "systems": systems,
            "found": statuses.get("found", 0), "with_system": sum(systems.values())}


def report(db: sqlite3.Connection) -> None:
    watched_systems = {s for s, _ in watched_boards()}
    watched = watched_boards()
    for list_name in [None, *LISTS]:
        s = summary(db, list_name)
        if not s["total"]:
            continue
        label = list_name or "all lists"
        print(f"\n  {label}: {s['total']:,} companies checked, {s['found']:,} careers pages, "
              f"{s['with_system']:,} with a recognised hiring system")
        print("    status   " + ", ".join(f"{k} {v:,}" for k, v in sorted(s["statuses"].items(), key=lambda x: -x[1])))
        print("    systems  " + ", ".join(f"{k} {v:,}" for k, v in s["systems"].items()))
        other = {k: v for k, v in s["systems"].items() if k not in watched_systems}
        print(f"    on systems boards.csv does not cover: {sum(other.values()):,}")
        where, params = ("AND list = ?", (list_name,)) if list_name else ("", ())
        unwatched = sum(1 for ats, slug in db.execute(
            f"SELECT DISTINCT ats, lower(ats_slug) FROM careers_sites WHERE status='found' AND ats_slug IS NOT NULL {where}",
            params) if ats in WATCHED_SYSTEMS and (ats, slug) not in watched)
        print(f"    boards on systems we scrape, not yet watched: {unwatched:,}")


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)          # atomic: a reader never sees half a file


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def running_pid() -> int | None:
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                               text=True, timeout=5).stdout.strip()
        if state.startswith("Z"):
            return None
    except (OSError, subprocess.SubprocessError):
        pass
    return pid


class Connectivity:
    """Is the internet there? Asked before a failure is believed."""

    def __init__(self):
        self._ok_at = 0.0
        self._lock = threading.Lock()

    def check(self) -> bool:
        with self._lock:
            if time.monotonic() - self._ok_at < 15:
                return True
        for address in (("1.1.1.1", 443), ("8.8.8.8", 443)):
            try:
                socket.create_connection(address, timeout=4).close()
                break
            except OSError:
                continue
        else:
            return False
        try:
            socket.getaddrinfo("example.com", 443)
        except OSError:
            return False
        with self._lock:
            self._ok_at = time.monotonic()
        return True


def _short(text: str, n: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def log_line(item: Item, outcome: Outcome) -> str:
    if outcome.status == "found":
        system = outcome.hit.system if outcome.hit else "own site"
        return f"{_short(item.name)} → {outcome.careers_url} ({system})"
    reason = _short(outcome.detail or "", 70)
    return f"{_short(item.name)} → {outcome.status}" + (f" ({reason})" if reason else "")


class Run:
    """One pass over one list: resumable, stoppable, and saying what it is doing."""

    def __init__(self, cfg, list_name: str, *, limit: int = 0, workers: int = 16, retry: bool = False,
                 verbose: bool = True):
        self.cfg = cfg
        self.list_name = list_name
        self.limit = limit
        self.workers = max(1, workers)
        self.retry = retry
        self.verbose = verbose
        self.stop = threading.Event()
        self.db = connect()
        self.connectivity = Connectivity()
        self.window: deque = deque()
        self.counts: Counter = Counter()
        self.systems: Counter = Counter()
        self.done_before = 0
        self.total = 0
        self.processed = 0
        self.found = 0
        self.last_progress = 0.0
        self.last_line = time.monotonic()
        self.last_export = time.monotonic()
        self.log_handle = None
        self.started = time.time()
        self.note = ""

    # -- output ----------------------------------------------------------
    def say(self, message: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  [{self.list_name}] {message}"
        print(line, flush=True)

    def runlog(self, line: str) -> None:
        if self.log_handle is None:
            RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
            if RUN_LOG.exists() and RUN_LOG.stat().st_size > 50_000_000:
                RUN_LOG.replace(RUN_LOG.with_suffix(".log.1"))
            self.log_handle = open(RUN_LOG, "a", encoding="utf-8", buffering=1)
        self.log_handle.write(f"{datetime.now().strftime('%H:%M:%S')}  {line}\n")

    def rate(self) -> float:
        if len(self.window) < 2:
            return 0.0
        (t0, n0), (t1, n1) = self.window[0], self.window[-1]
        return (n1 - n0) / (t1 - t0) * 3600 if t1 > t0 else 0.0

    def eta(self) -> str | None:
        rate = self.rate()
        left = self.total - (self.done_before + self.processed)
        if self.limit:
            left = min(left, self.limit - self.processed)
        if rate <= 0 or left <= 0:
            return None
        hours = left / rate
        finish = datetime.now(timezone.utc) + timedelta(hours=hours)
        span = f"{hours / 24:.1f} days" if hours >= 48 else f"{int(hours)}h {int(hours % 1 * 60)}m"
        return f"about {span} (around {finish.strftime('%Y-%m-%d %H:%M')} UTC)"

    def progress(self, phase: str | None = None, note: str | None = None, final: bool = False) -> None:
        if note:
            self.note = note
        rate = self.rate()
        data = {
            "phase": phase or f"running {self.list_name}",
            "list": self.list_name,
            "processed": self.done_before + self.processed,
            "total": self.total,
            "careers_found": self.found,
            "ats_found": dict(self.systems.most_common()),
            "rate_per_hour": round(rate) if rate else None,
            "eta": None if final else self.eta(),
            "background_pid": None if final else os.getpid(),
            "log": str(RUN_LOG.relative_to(ROOT)),
            "updated_at": utcnow(),
            "note": self.note,
        }
        _write_json(PROGRESS, data)
        state = read_json(STATE)
        state.setdefault("lists", {})[self.list_name] = {
            "status": "finished" if final and phase == "done" else ("stopped" if final else "running"),
            "done": data["processed"], "total": self.total, "found": self.found,
            "per_hour": data["rate_per_hour"], "eta": data["eta"], "updated_at": data["updated_at"],
            "pid": None if final else os.getpid(),
        }
        state["current"] = None if final else self.list_name
        _write_json(STATE, state)
        self.last_progress = time.monotonic()

    # -- the pass ---------------------------------------------------------
    def _pending(self) -> list[Item]:
        items = build_items(self.list_name, self.db)
        self.all_names = [i.name for i in items]
        self.total = len(items)
        done = {k: s for k, s in self.db.execute(
            "SELECT item_key, status FROM careers_sites WHERE list = ?", (self.list_name,))}
        if self.retry:
            done = {k: s for k, s in done.items() if s not in RETRYABLE}
        pending = [i for i in items if i.key not in done]
        self.done_before = self.total - len(pending)
        s = summary(self.db, self.list_name)
        if self.retry:
            retried = {i.key for i in pending}
            rows = self.db.execute("SELECT item_key, status, ats FROM careers_sites WHERE list = ?",
                                   (self.list_name,)).fetchall()
            rows = [r for r in rows if r[0] not in retried]
            self.found = sum(1 for r in rows if r[1] == "found")
            self.systems = Counter(r[2] for r in rows if r[1] == "found" and r[2])
        else:
            self.found = s["found"]
            self.systems = Counter(s["systems"])
        if self.limit:
            pending = pending[: self.limit]
        return pending

    def run(self) -> int:
        if running_pid() and running_pid() != os.getpid():
            print(f"  already running as pid {running_pid()} -- `python main.py careers --stop` first")
            return 1
        STOPFILE.unlink(missing_ok=True)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        PIDFILE.write_text(f"{os.getpid()}\n")
        awake = None
        try:
            awake = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        try:
            return self._run()
        finally:
            PIDFILE.unlink(missing_ok=True)
            STOPFILE.unlink(missing_ok=True)
            if awake is not None:
                awake.terminate()
            if self.log_handle:
                self.log_handle.close()

    def _on_signal(self, signum, frame) -> None:
        if not self.stop.is_set():
            self.say("stop requested -- finishing the companies under way, keeping everything saved")
        self.stop.set()

    def _run(self) -> int:
        self.say("loading the list")
        self.progress(note=f"Loading the {self.list_name} list.")
        pending = self._pending()
        index = None
        if self.list_name in ("postings", "watched"):
            index = DomainIndex()
            index.load_registry(self.db)
            if pending:
                # The whole list, not just this batch: the dump is a gigabyte, read it once.
                self.progress(note="Matching company names to domains (a minute or two, once).")
                index.prepare(self.all_names, say=self.say)
        left = len(pending)
        self.say(f"{self.total:,} companies, {self.done_before:,} already answered, {left:,} to go"
                 f" with {self.workers} workers")
        self.progress(note=f"Checking {left:,} {self.list_name} companies for careers pages.")
        if not pending:
            return self._finish(stopped=False)

        client = make_client(self.cfg)
        finder = Finder(self._fetcher(client), index, stop=self.stop)
        queue = deque(pending)
        buffer: list[tuple] = []
        self.window.append((time.time(), 0))
        offline_wait = 30

        def work(item: Item):
            client.local.net_errors = 0
            wall, mono = time.time(), time.monotonic()
            try:
                outcome = finder.find(item)
            except Exception as exc:          # one bad page must not end a multi-day run
                tb = traceback.extract_tb(exc.__traceback__)[-1]
                outcome = Outcome("error", detail=f"{type(exc).__name__} at {Path(tb.filename).name}:{tb.lineno}")
            outcome.net_errors = getattr(client.local, "net_errors", 0)
            slept = (time.time() - wall) - (time.monotonic() - mono) > 30
            return item, outcome, slept

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            running: dict = {}
            while (queue or running) and not self.stop.is_set():
                if STOPFILE.exists():
                    self._on_signal(None, None)
                    break
                while queue and len(running) < self.workers * 2 and not self.stop.is_set():
                    item = queue.popleft()
                    running[pool.submit(work, item)] = item
                done, _ = wait(list(running), timeout=5, return_when=FIRST_COMPLETED)
                requeue = []
                for future in done:
                    running.pop(future)
                    item, outcome, slept = future.result()
                    if outcome.cut_short and self.stop.is_set():
                        continue                      # cut short by the stop: asked again next time
                    if outcome.status != "found" and (outcome.net_errors or outcome.status == "error") \
                            and (slept or not self.connectivity.check()):
                        requeue.append(item)          # the Mac slept or the wifi dropped
                        continue
                    buffer.append(to_row(item, outcome))
                    self.processed += 1
                    self.counts[outcome.status] += 1
                    if outcome.status == "found":
                        self.found += 1
                        if outcome.hit:
                            self.systems[outcome.hit.system] += 1
                    self.runlog(log_line(item, outcome))
                if requeue:
                    queue.extendleft(reversed(requeue))
                    while not self.connectivity.check() and not self.stop.is_set():
                        self.say(f"no internet -- waiting {offline_wait}s; nothing is recorded meanwhile")
                        self.progress(note="Waiting for the internet to come back.")
                        self.stop.wait(offline_wait)
                        offline_wait = min(offline_wait * 2, 900)
                    offline_wait = 30
                if buffer and (len(buffer) >= 25 or not running):
                    save(self.db, buffer)
                    buffer.clear()
                self._tick()
            if buffer:
                save(self.db, buffer)
                buffer.clear()
            if self.stop.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
                return self._finish(stopped=True, hard=True)
        return self._finish(stopped=False)

    def _fetcher(self, client: CareersClient):
        def fetch(url: str, **kw) -> Page:
            return client.fetch(url, **kw)
        return fetch

    def _tick(self) -> None:
        now = time.time()
        total_done = self.processed
        if not self.window or now - self.window[-1][0] >= 20:
            self.window.append((now, total_done))
            while len(self.window) > 2 and self.window[0][0] < now - 30 * 60:
                self.window.popleft()
        if time.monotonic() - self.last_progress >= 15:
            self.progress(note=f"Checking {self.list_name} companies for careers pages and hiring systems.")
        if time.monotonic() - self.last_line >= 60:
            rate = self.rate()
            eta = self.eta()
            self.say(f"{self.done_before + self.processed:,}/{self.total:,} · careers pages {self.found:,} · "
                     f"{rate:,.0f}/h" + (f" · {eta}" if eta else ""))
            self.last_line = time.monotonic()
        if time.monotonic() - self.last_export >= 30 * 60:
            export(self.db)
            self._watch_new_boards()
            self.last_export = time.monotonic()

    def _watch_new_boards(self) -> None:
        try:
            added = watch_new_boards(self.cfg)
        except Exception as exc:          # the fetch list is a bonus: a locked database must not end the run
            self.say(f"could not add new boards to the daily fetch ({type(exc).__name__}); next export tries again")
            return
        if added:
            self.say(f"{added:,} new board{'' if added == 1 else 's'} added to the daily fetch")
            self.runlog(f"{added:,} new board{'' if added == 1 else 's'} added to the daily fetch")

    def _finish(self, stopped: bool, hard: bool = False) -> int:
        rows, new = export(self.db)
        self._watch_new_boards()
        done = self.done_before + self.processed
        if stopped:
            self.say(f"stopped at {done:,}/{self.total:,}; run the same command to resume")
            self.progress(phase=f"running {self.list_name}", final=True,
                          note=f"Paused at {done:,} of {self.total:,}; it resumes where it stopped.")
        else:
            self.say(f"finished: {done:,}/{self.total:,} companies, {self.found:,} careers pages; "
                     f"{rows:,} rows in {CSV_PATH.relative_to(ROOT)}, {new:,} unwatched boards in "
                     f"{NEW_BOARDS_CSV.relative_to(ROOT)}")
            self.progress(phase="done", final=True,
                          note=f"Finished {self.list_name}: {self.found:,} careers pages among {self.total:,} companies.")
        if hard:
            # Workers still inside a page would hold the exit for minutes; all
            # answers are saved, and their companies are simply asked next time.
            if self.log_handle:
                self.log_handle.close()
            PIDFILE.unlink(missing_ok=True)
            STOPFILE.unlink(missing_ok=True)
            os._exit(0)
        return 0


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def status() -> int:
    state = read_json(STATE)
    pid = running_pid()
    print(f"  running: {'yes, pid %d' % pid if pid else 'no'}")
    for name, s in (state.get("lists") or {}).items():
        print(f"  {name:11} {s.get('status', '?'):9} {s.get('done', 0):,}/{s.get('total', 0):,}"
              f"  careers pages {s.get('found', 0):,}"
              + (f"  {s['per_hour']:,}/h" if s.get("per_hour") else "")
              + (f"  {s['eta']}" if s.get("eta") and pid else "")
              + f"  ({s.get('updated_at', '?')})")
    return 0


def stop() -> int:
    pid = running_pid()
    if not pid:
        print("  not running")
        return 0
    STOPFILE.write_text(utcnow())
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    print(f"  asked pid {pid} to stop; everything answered so far is kept")
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--list", default="postings",
                        help="postings | watched | registry | worthwhile | small; "
                             "comma-separate to run several in turn")
    parser.add_argument("--limit", type=int, default=0, help="Only this many new companies")
    parser.add_argument("--workers", type=int, default=16,
                        help="Companies checked side by side (each host is still paced on its own)")
    parser.add_argument("--retry", action="store_true", help="Ask again where the last answer was an error")
    parser.add_argument("--status", action="store_true", help="What the finder is doing")
    parser.add_argument("--stop", action="store_true", help="Stop a running finder; it resumes later")
    parser.add_argument("--export", action="store_true", help="Rewrite data/careers/careers_sites.csv")
    parser.add_argument("--report", action="store_true", help="Counts by status and hiring system")


def run_command(cfg, args) -> int:
    if args.status:
        return status()
    if args.stop:
        return stop()
    if args.export or args.report:
        db = connect()
        if args.export:
            rows, new = export(db)
            print(f"  {rows:,} rows -> {CSV_PATH.relative_to(ROOT)}; {new:,} boards -> "
                  f"{NEW_BOARDS_CSV.relative_to(ROOT)}")
        if args.report:
            report(db)
        return 0
    lists = [s.strip() for s in args.list.split(",") if s.strip()]
    for name in lists:
        if name not in LISTS:
            print(f"  unknown list {name!r}: choose from {', '.join(LISTS)}")
            return 2
    for name in lists:
        code = Run(cfg, name, limit=args.limit, workers=args.workers, retry=args.retry).run()
        if code:
            return code
    return 0


def main(argv: list[str] | None = None) -> int:
    from backend.config.loader import load_config

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    args = parser.parse_args(argv)
    return run_command(load_config(), args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
