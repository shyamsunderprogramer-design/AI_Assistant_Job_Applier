"""Polite HTTP client: robots.txt enforcement, per-host rate limiting, backoff.

See README.md §C5. Do not weaken these defaults to go faster.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)


class RobotsDisallowed(Exception):
    """Raised when robots.txt forbids a URL. Caller logs and skips."""


class FetchError(Exception):
    """Non-retryable or retry-exhausted HTTP failure."""


class NetworkError(FetchError):
    """No answer at all: the name did not resolve, the connection was refused or reset, or it timed out.

    Not an answer about the board. A board is retired only on what its host SAID (a 404, a page that is not a
    board, a refusal of access), never on silence -- an outage once retired healthy boards whose last "failure"
    was the machine having no internet (data_engineering/scraper/runner.py)."""


class RateLimited(FetchError):
    """The host is refusing us with 429s. Not an answer about the URL.

    A subclass so every existing `except FetchError` still treats it as a
    failure, but callers that record answers -- slug probing above all -- must
    let it through rather than read it as "this board does not exist". That
    reading filed hundreds of Workable companies as board-less on 23 Sep 2026,
    on a day Workable was refusing every request.
    """


@dataclass
class HttpSettings:
    user_agent: str
    respect_robots: bool = True
    timeout_seconds: int = 20
    min_delay_seconds: float = 1.5
    jitter_seconds: float = 0.75
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    # After a host has 429'd a request through every retry, stop asking it for
    # this long. Retrying each URL in turn cost about four minutes per board
    # and turned a one-hour daily run into nine and a half.
    rate_limit_cooldown_seconds: float = 900.0
    # Treat a 401/403 on robots.txt as a blanket disallow. Stricter than
    # RFC 9309, which says a 4xx means the file is unavailable and the crawler
    # may proceed. Off by default; see _robots_for().
    strict_robots_on_4xx: bool = False
    # Hosts that want a longer gap than min_delay_seconds: apply.workable.com
    # turned away 56 boards in one run at 1.5s.
    host_delays: dict[str, float] = field(default_factory=dict)
    # The longest a Retry-After is honoured in one sleep (Nov 2026: also as an HTTP date). Longer asks are
    # not slept through here: the request gives up, and the host cools off (below).
    max_retry_after_seconds: float = 60.0
    # Backoff between retries grows by `backoff_base_seconds ** attempt`, plus up to this fraction of it at
    # random, so several workers refused at once do not all come back in the same instant.
    backoff_jitter_fraction: float = 0.5
    # Providers that give each employer its own host but answer from shared infrastructure (Workday:
    # acme.wd5.myworkdayjobs.com). One tenant's 429 says little; several different tenants refusing every
    # retry says the provider is throttling us, so then EVERY host of that provider cools off together --
    # never asked faster by being asked through another tenant (Discovery partitions, Nov 2026).
    provider_groups: dict[str, list[str]] = field(default_factory=lambda: {
        "workday": ["myworkdayjobs.com", "myworkdaysite.com"]})
    provider_trip_hosts: int = 3
    # The least time between ANY two requests of this client, whatever their hosts (26 Nov 2026): how a site's
    # total request rate is held under its cap across all its readers and runners (devops/discovery_plan.py gives
    # each reader its share: readers / max_rps). 0: only the per-host gap, as before.
    min_interval_seconds: float = 0.0

    @classmethod
    def from_config(cls, cfg) -> "HttpSettings":
        return cls(
            user_agent=cfg.user_agent,
            respect_robots=bool(cfg.get("http.respect_robots", True)),
            timeout_seconds=int(cfg.get("http.timeout_seconds", 20)),
            min_delay_seconds=float(cfg.get("http.min_delay_seconds", 1.5)),
            jitter_seconds=float(cfg.get("http.jitter_seconds", 0.75)),
            max_retries=int(cfg.get("http.max_retries", 3)),
            backoff_base_seconds=float(cfg.get("http.backoff_base_seconds", 2.0)),
            rate_limit_cooldown_seconds=float(
                cfg.get("http.rate_limit_cooldown_seconds", 900.0)),
            strict_robots_on_4xx=bool(cfg.get("http.strict_robots_on_4xx", False)),
            host_delays={str(h): float(s) for h, s in (cfg.get("http.host_delays", {}) or {}).items()},
            max_retry_after_seconds=float(cfg.get("http.max_retry_after_seconds", 60.0)),
            backoff_jitter_fraction=float(cfg.get("http.backoff_jitter_fraction", 0.5)),
            provider_groups={str(g): [str(x) for x in (v or [])] for g, v in
                             (cfg.get("http.provider_groups", None) or {
                                 "workday": ["myworkdayjobs.com", "myworkdaysite.com"]}).items()},
            provider_trip_hosts=int(cfg.get("http.provider_trip_hosts", 3)),
            min_interval_seconds=float(cfg.get("http.min_interval_seconds", 0) or 0),
        )


class PoliteClient:
    """Wraps requests with per-host pacing and robots.txt checks."""

    def __init__(self, settings: HttpSettings):
        self.settings = settings
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": settings.user_agent})
        self._last_request_at: dict[str, float] = {}
        # A lock per host, created on demand; `_locks_guard` only protects the
        # creation, never the wait itself.
        self._host_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        # host -> monotonic time before which we do not ask it anything
        self._cooling_until: dict[str, float] = {}
        # host -> refusals in a row; each one doubles the next cool-off
        self._refusals: dict[str, int] = {}
        # provider group -> (monotonic time before which none of its hosts is asked, its hosts that refused
        # every retry, refusals in a row of the group). See HttpSettings.provider_groups.
        self._group_cooling_until: dict[str, float] = {}
        self._group_refused_hosts: dict[str, set[str]] = {}
        self._group_refusals: dict[str, int] = {}
        # For the run's report: how often each host and provider group turned us away (HTTP 429).
        self.rate_limit_events: dict[str, int] = {}
        self._rate_guard = threading.Lock()
        # The site-wide share (HttpSettings.min_interval_seconds), and how many requests were sent, for the report.
        self._any_lock = threading.Lock()
        self._last_any: float | None = None
        self.requests_made = 0

    # -- robots ------------------------------------------------------------
    def _robots_for(self, host_root: str) -> urllib.robotparser.RobotFileParser | None:
        """Fetch and cache robots.txt for a scheme://host. None = unavailable."""
        if host_root in self._robots:
            return self._robots[host_root]

        parser = urllib.robotparser.RobotFileParser()
        robots_url = f"{host_root}/robots.txt"
        try:
            # Counted, and held to the site-wide interval like any request (26 Nov 2026): on a site with a host per
            # employer (BambooHR, JazzHR, Breezy...) it is one per board read. Not to the per-host gap: robots.txt and
            # the page after it are the crawler's one visit (the gap is kept between that host's own requests).
            self._wait_site()
            with self._rate_guard:
                self.requests_made += 1
            resp = self._session.get(robots_url, timeout=self.settings.timeout_seconds)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            elif 400 <= resp.status_code < 500:
                # RFC 9309 §2.3.1.3 ("Unavailable" Status): a 4xx means the
                # robots.txt file is unavailable, and "the crawler MAY access
                # any resources on the server". 401 and 403 are not exceptions
                # — an unreadable robots.txt is an ABSENT one, not a blanket
                # Disallow. Google's crawler documents the same reading.
                #
                # This code previously did the opposite while citing this very
                # RFC, and Python's stdlib RobotFileParser has the same
                # non-compliant behaviour, which is probably where it came from.
                # It made api.ashbyhq.com (401 on /robots.txt, but a documented
                # PUBLIC job-board API) unscrapeable.
                #
                # Set http.strict_robots_on_4xx: true to restore the cautious
                # reading — it is stricter than the standard, not politer than
                # it, and it will silently exclude hosts that permit crawling.
                if resp.status_code in (401, 403) and self.settings.strict_robots_on_4xx:
                    log.info(
                        "robots.txt for %s returned %d; strict mode treats that as "
                        "full disallow", host_root, resp.status_code,
                    )
                    parser.parse(["User-agent: *", "Disallow: /"])
                else:
                    parser.parse([])
            else:
                # 5xx. RFC 9309 §2.3.1.4: unreachable means assume complete
                # disallow. A server having a bad day is not permission.
                log.warning(
                    "robots.txt for %s returned %d — assuming disallow until it recovers",
                    host_root, resp.status_code,
                )
                parser.parse(["User-agent: *", "Disallow: /"])
        except requests.RequestException as exc:
            log.warning("robots.txt unreachable for %s (%s) — proceeding politely", host_root, exc)
            self._robots[host_root] = None
            return None

        self._robots[host_root] = parser
        return parser

    def allowed(self, url: str) -> bool:
        if not self.settings.respect_robots:
            return True
        parsed = urlparse(url)
        host_root = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots_for(host_root)
        if parser is None:
            return True
        return parser.can_fetch(self.settings.user_agent, url)

    # -- pacing ------------------------------------------------------------
    def _wait_turn(self, host: str) -> None:
        """Hold back until this host's turn comes round again.

        One lock per host, not one lock overall. The delay has always been
        per-host, but the scraper walked boards one at a time, so four
        different hosts took turns waiting on each other for no reason: an
        hour and forty minutes of which only forty-six were greenhouse.io
        actually being paced. Holding the lock across the sleep is what makes
        concurrency safe -- two workers on the same host still queue, and two
        workers on different hosts never meet.
        """
        with self._locks_guard:
            lock = self._host_locks.setdefault(host, threading.Lock())

        with lock:
            delay = (self.settings.host_delays.get(host, self.settings.min_delay_seconds)
                     + random.uniform(0, self.settings.jitter_seconds))
            last = self._last_request_at.get(host)
            if last is not None:
                elapsed = time.monotonic() - last
                if elapsed < delay:
                    time.sleep(delay - elapsed)
            self._last_request_at[host] = time.monotonic()
        self._wait_site()

    def _wait_site(self) -> None:
        """The site-wide share (HttpSettings.min_interval_seconds): at least this long between ANY two requests."""
        gap = self.settings.min_interval_seconds
        if gap > 0:
            with self._any_lock:
                if self._last_any is not None:
                    elapsed = time.monotonic() - self._last_any
                    if elapsed < gap:
                        time.sleep(gap - elapsed)
                self._last_any = time.monotonic()

    # -- requests ----------------------------------------------------------
    def get(self, url: str, **kwargs) -> requests.Response:
        """GET with robots check, pacing, and bounded backoff on 429/5xx."""
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        """POST under exactly the same rules as GET.

        Some boards only answer a search over POST — Workday's career-site API
        is one. Sharing `_request` means a new verb cannot quietly skip the
        robots check or the pacing.
        """
        return self._request("POST", url, **kwargs)

    # Board discovery asks a different host for every company on the boards
    # that give each one a subdomain (BambooHR, Breezy, JazzHR, Recruitee,
    # Teamtailor). Kept for a whole run, their cookies and robots.txt files
    # piled up past a hundred thousand; the cookie jar is searched on every
    # request, so each probe got slower than the last -- 1,084 names an hour
    # fell to 225 with one core pinned. Past these sizes they are let go.
    MAX_COOKIES = 2000
    MAX_ROBOTS = 5000

    def group_of(self, host: str) -> str | None:
        """The provider group whose hosts `host` belongs to (HttpSettings.provider_groups), or None."""
        host = (host or "").lower()
        for group, suffixes in (self.settings.provider_groups or {}).items():
            if any(host == s or host.endswith("." + s) for s in suffixes):
                return group
        return None

    def cooling_left(self, host: str) -> float:
        """Seconds until a host that refused us may be asked again (0 if it may now) -- the longer of its own
        cool-off and its provider group's."""
        now = time.monotonic()
        own = self._cooling_until.get(host, 0.0) - now
        group = self.group_of(host)
        shared = (self._group_cooling_until.get(group, 0.0) - now) if group else 0.0
        return max(0.0, own, shared)

    def group_cooling_left(self, group: str) -> float:
        return max(0.0, self._group_cooling_until.get(group, 0.0) - time.monotonic())

    @staticmethod
    def retry_after_seconds(value) -> float | None:
        """A Retry-After header as seconds: delta-seconds or an HTTP date (RFC 9110 §10.2.3); None if absent
        or unreadable. Never negative."""
        if value is None:
            return None
        text = str(value).strip()
        if text.isdigit():
            return float(text)
        try:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone
            when = parsedate_to_datetime(text)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, IndexError, OverflowError):
            return None

    def _note_refusal(self, host: str) -> None:
        with self._rate_guard:
            self.rate_limit_events[host] = self.rate_limit_events.get(host, 0) + 1
            group = self.group_of(host)
            if group:
                key = f"provider:{group}"
                self.rate_limit_events[key] = self.rate_limit_events.get(key, 0) + 1

    def _forget_old(self) -> None:
        if len(self._session.cookies) > self.MAX_COOKIES:
            self._session.cookies.clear()
        if len(self._robots) > self.MAX_ROBOTS:
            self._robots.clear()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        self._forget_old()
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        host = urlparse(url).netloc
        last_exc: Exception | None = None
        throttled = False
        asked_wait = 0.0          # the longest Retry-After this request was given: a cool-off is never shorter

        for attempt in range(self.settings.max_retries + 1):
            # Checked every attempt: another worker may have tripped it while
            # this one was sleeping.
            if time.monotonic() < self._cooling_until.get(host, 0.0):
                raise RateLimited(f"{host} is rate-limiting us; not asking until it cools off")
            group = self.group_of(host)
            if group and time.monotonic() < self._group_cooling_until.get(group, 0.0):
                raise RateLimited(f"{group} is rate-limiting us; none of its hosts is asked until it cools off")
            self._wait_turn(host)
            try:
                with self._rate_guard:
                    self.requests_made += 1
                resp = self._session.request(
                    method, url, timeout=self.settings.timeout_seconds, **kwargs
                )
            except requests.RequestException as exc:
                last_exc = exc
                throttled = False
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    throttled = resp.status_code == 429
                    if throttled:
                        self._note_refusal(host)
                    last_exc = FetchError(f"HTTP {resp.status_code} for {url}")
                    wait = self.retry_after_seconds(resp.headers.get("Retry-After"))
                    if wait is not None:
                        asked_wait = max(asked_wait, wait)
                    if wait is not None and attempt < self.settings.max_retries:
                        if wait > self.settings.max_retry_after_seconds:
                            # Asked to stay away longer than one sleep here: honoured by not asking again
                            # now -- the host cools off below -- rather than by a worker asleep for it.
                            break
                        time.sleep(wait)
                        continue
                else:
                    self._refusals.pop(host, None)   # it answered: forgive the past
                    group = self.group_of(host)
                    if group:
                        with self._rate_guard:
                            self._group_refused_hosts.get(group, set()).discard(host)
                            self._group_refusals.pop(group, None)
                    return resp

            if attempt < self.settings.max_retries:
                backoff = self.settings.backoff_base_seconds ** (attempt + 1)
                backoff += random.uniform(0, backoff * max(0.0, self.settings.backoff_jitter_fraction))
                log.debug("Retry %d for %s in %.1fs (%s)", attempt + 1, url, backoff, last_exc)
                time.sleep(backoff)

        if throttled:
            # Doubling, not fixed: Workable refused every request for four days
            # (23-26 Sep 2026), and a fixed 15 minutes meant four stalls an
            # hour spent being refused again. A host that keeps saying no is
            # asked less and less often -- 15 min, 30, 1 h ... 6 h at most --
            # and one successful answer resets it.
            n = self._refusals.get(host, 0) + 1
            self._refusals[host] = n
            cooldown = min(self.settings.rate_limit_cooldown_seconds * 2 ** (n - 1), 6 * 3600)
            cooldown = max(cooldown, asked_wait)          # never back sooner than the host asked
            self._cooling_until[host] = time.monotonic() + cooldown
            log.warning("%s refused every retry with HTTP 429 — leaving it alone for %.0f min",
                        host, cooldown / 60)
            group = self.group_of(host)
            if group:
                with self._rate_guard:
                    refused = self._group_refused_hosts.setdefault(group, set())
                    refused.add(host)
                    if len(refused) >= max(1, self.settings.provider_trip_hosts):
                        g = self._group_refusals.get(group, 0) + 1
                        self._group_refusals[group] = g
                        pause = min(self.settings.rate_limit_cooldown_seconds * 2 ** (g - 1), 6 * 3600)
                        self._group_cooling_until[group] = time.monotonic() + pause
                        refused.clear()
                        log.warning("%s: %d of its hosts refused every retry -- leaving the whole provider "
                                    "alone for %.0f min", group, self.settings.provider_trip_hosts, pause / 60)
            raise RateLimited(f"Giving up on {url}: {host} is rate-limiting us (HTTP 429)")
        if isinstance(last_exc, (requests.ConnectionError, requests.Timeout)):
            raise NetworkError(f"Giving up on {url} after {self.settings.max_retries} retries: {last_exc}")
        raise FetchError(f"Giving up on {url} after {self.settings.max_retries} retries: {last_exc}")

    def get_json(self, url: str, **kwargs) -> object:
        resp = self.get(url, **kwargs)
        if resp.status_code == 404:
            raise FetchError(f"HTTP 404 for {url}")
        resp.raise_for_status()
        return resp.json()

    def head_ok(self, url: str) -> bool:
        """True if the URL returns a 2xx. Used by slug probing."""
        try:
            resp = self.get(url)
        except (FetchError, RobotsDisallowed):
            return False
        return 200 <= resp.status_code < 300


# Two well-known hosts, asked only whether they answer at all (a name lookup and a TCP connect -- nothing is sent).
PROBE_HOSTS = (("github.com", 443), ("www.cloudflare.com", 443))


def network_ok(hosts=PROBE_HOSTS, timeout: float = 4.0) -> bool:
    """Whether this machine can reach the internet at all: True as soon as one probe host accepts a connection.
    Tells "this board's host is silent" apart from "nothing answers" (runner.NetworkWatch; devops/cloud_results)."""
    import socket
    for host, port in hosts:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False
