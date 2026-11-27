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


class ServerError(FetchError):
    """The host answered 5xx through every retry: trouble on its side, not an answer about the board -- it never
    counts toward retiring one (data_engineering/scraper/runner.py), as it once retired four Workable boards."""


class RobotsUnavailable(FetchError):
    """robots.txt could not be read just now (a 5xx, a timeout, no connection, an unexpected 4xx): RFC 9309
    §2.3.1.4 -- nothing on that host is asked until it can be. Not an answer about the board either."""


# robots.txt answers (27 Nov 2026). Only a missing file (404, 410) means "no rules" (RFC 9309 §2.3.1.3); a refusal to
# show the rules (401, 403, 451) is never read as permission; anything else (429, 5xx, other 4xx, no answer) is
# "unavailable": nothing is asked of the host for now, and the answer is never kept for later runs.
ROBOTS_ABSENT = (404, 410)
ROBOTS_REFUSED = (401, 403, 451)
ROBOTS_KEEP = (200,) + ROBOTS_ABSENT + ROBOTS_REFUSED      # definite answers: the only ones cached (24 hours at most)
ROBOTS_RETRY_SECONDS = 300.0       # an unavailable robots.txt is asked again after this long, never before
ROBOTS_MAX_BYTES = 512_000         # RFC 9309 §2.5: parse at least 500 KiB, so a long file's last rules are kept


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
    # Hosts whose robots.txt refuses to be read (401/403) but whose owner DOCUMENTS the API as public for anyone
    # to read: api.ashbyhq.com (Ashby's public Job Posting API, developers.ashbyhq.com). Every other refusal is a
    # full disallow (27 Nov 2026) -- a 401 or 403 is never read as permission on its own.
    robots_public_api_hosts: tuple = ("api.ashbyhq.com",)
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
            robots_public_api_hosts=tuple(str(h).lower() for h in (
                cfg.get("http.robots_public_api_hosts", None) or ("api.ashbyhq.com",))),
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
        # robots.txt read in an earlier run (preload_robots) and read in this one (handed on to the next runs).
        self._robots_known: dict[str, tuple] = {}
        self.robots_seen: dict[str, tuple] = {}           # host_root -> (status, text, fetched at, UTC ISO)
        # host_root -> (monotonic time before which its robots.txt is not asked again, "unreachable"/"rate_limited")
        self._robots_unavailable: dict[str, tuple[float, str]] = {}
        # The longest Retry-After any host gave this client (seconds): kept by the merge as the provider's cool-off.
        self.longest_retry_after = 0.0

    # -- robots ------------------------------------------------------------
    def preload_robots(self, known: dict) -> None:
        """robots.txt answers read in the last 24 hours (RFC 9309 §2.4 allows caching that long), as
        {scheme://host: (status, text)}: used instead of asking again (devops/discovery_worker.py)."""
        self._robots_known.update(known or {})

    def _robots_for(self, host_root: str):
        """The robots.txt rules for a scheme://host, read once per run (or from the last day's answers); or
        "unreachable" / "rate_limited" while it cannot be read -- then nothing on the host is asked."""
        if host_root in self._robots:
            return self._robots[host_root]
        down = self._robots_unavailable.get(host_root)
        if down is not None:
            if time.monotonic() < down[0]:
                return down[1]
            del self._robots_unavailable[host_root]
        known = self._robots_known.get(host_root)
        if known is not None and int(known[0]) in ROBOTS_KEEP:
            self._robots[host_root] = self._parse_robots(host_root, int(known[0]), known[1] or "")
            return self._robots[host_root]

        robots_url = f"{host_root}/robots.txt"
        host = urlparse(host_root).netloc
        try:
            # Counted, and held to the site-wide interval like any request (26 Nov 2026): on a site with a host per
            # employer (BambooHR, JazzHR, Breezy...) it is one per board read. Not to the per-host gap: robots.txt and
            # the page after it are the crawler's one visit (the gap is kept between that host's own requests).
            self._wait_site()
            with self._rate_guard:
                self.requests_made += 1
            fetched_at = _utc_now()
            resp = self._session.get(robots_url, timeout=self.settings.timeout_seconds)
        except requests.RequestException as exc:
            log.warning("robots.txt unreachable for %s (%s) -- nothing there is asked until it answers", host_root, exc)
            return self._robots_down(host_root, "unreachable", ROBOTS_RETRY_SECONDS)
        status = int(resp.status_code)
        if status == 429:
            # Refused: the host cools off as after any 429 (never sooner than its Retry-After), and the boards on it
            # are deferred, not failed.
            self._note_refusal(host)
            wait = self.retry_after_seconds((getattr(resp, "headers", None) or {}).get("Retry-After")) or 0.0
            self._cool_off(host, wait)
            return self._robots_down(host_root, "rate_limited", max(ROBOTS_RETRY_SECONDS, wait))
        if status not in ROBOTS_KEEP:
            log.warning("robots.txt for %s returned %d -- unavailable: nothing there is asked until it answers",
                        host_root, status)
            return self._robots_down(host_root, "unreachable", ROBOTS_RETRY_SECONDS)
        text = (resp.text or "")[:ROBOTS_MAX_BYTES] if status == 200 else ""
        parser = self._parse_robots(host_root, status, text)
        with self._rate_guard:
            self.robots_seen[host_root] = (status, text, fetched_at)    # a definite answer, kept 24 hours at most
        self._robots[host_root] = parser
        return parser

    def _robots_down(self, host_root: str, why: str, seconds: float) -> str:
        self._robots_unavailable[host_root] = (time.monotonic() + float(seconds), why)
        return why

    def _parse_robots(self, host_root: str, status: int, text: str) -> urllib.robotparser.RobotFileParser:
        parser = urllib.robotparser.RobotFileParser()
        if status == 200:
            parser.parse(text.splitlines())
        elif status in ROBOTS_ABSENT:
            parser.parse([])                   # RFC 9309 §2.3.1.3: no robots.txt, no rules
        elif status in ROBOTS_REFUSED and urlparse(host_root).netloc.lower() in self.settings.robots_public_api_hosts:
            # The owner documents this API as public (HttpSettings.robots_public_api_hosts): that, not the refusal,
            # is the permission.
            parser.parse([])
        else:
            # A refusal to show the rules (401, 403, 451) is not permission, and nothing else reaches here: the
            # whole host is disallowed (27 Nov 2026; until then a 401/403 read as "no robots.txt").
            if status in ROBOTS_REFUSED:
                log.info("robots.txt for %s returned %d: treated as a full disallow", host_root, status)
            parser.parse(["User-agent: *", "Disallow: /"])
        return parser

    def robots_check(self, url: str) -> None:
        """Raises unless robots.txt lets us ask for `url`: RobotsDisallowed (the owner's rules say no),
        RateLimited (robots.txt itself was refused with a 429) or RobotsUnavailable (it cannot be read now)."""
        if not self.settings.respect_robots:
            return
        parsed = urlparse(url)
        host_root = f"{parsed.scheme}://{parsed.netloc}"
        rules = self._robots_for(host_root)
        if rules == "rate_limited":
            raise RateLimited(f"{parsed.netloc} refused its robots.txt (HTTP 429); not asking until it cools off")
        if isinstance(rules, str):
            raise RobotsUnavailable(f"robots.txt for {host_root} cannot be read now; nothing there is asked")
        if not rules.can_fetch(self.settings.user_agent, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

    def allowed(self, url: str) -> bool:
        try:
            self.robots_check(url)
        except (RobotsDisallowed, FetchError):
            return False
        return True

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
        host = urlparse(url).netloc
        # A host cooling off is not asked anything -- its robots.txt included -- until the cool-off ends.
        if time.monotonic() < self._cooling_until.get(host, 0.0):
            raise RateLimited(f"{host} is rate-limiting us; not asking until it cools off")
        group = self.group_of(host)
        if group and time.monotonic() < self._group_cooling_until.get(group, 0.0):
            raise RateLimited(f"{group} is rate-limiting us; none of its hosts is asked until it cools off")
        self.robots_check(url)

        last_exc: Exception | None = None
        last_status = 0
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
                last_status = 0
                throttled = False
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    throttled = resp.status_code == 429
                    last_status = resp.status_code
                    if throttled:
                        self._note_refusal(host)
                    last_exc = FetchError(f"HTTP {resp.status_code} for {url}")
                    wait = self.retry_after_seconds(resp.headers.get("Retry-After"))
                    if wait is not None:
                        asked_wait = max(asked_wait, wait)
                        with self._rate_guard:
                            self.longest_retry_after = max(self.longest_retry_after, wait)
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
            self._cool_off(host, asked_wait)
            raise RateLimited(f"Giving up on {url}: {host} is rate-limiting us (HTTP 429)")
        if isinstance(last_exc, (requests.ConnectionError, requests.Timeout)):
            raise NetworkError(f"Giving up on {url} after {self.settings.max_retries} retries: {last_exc}")
        if last_status >= 500:
            raise ServerError(f"Giving up on {url} after {self.settings.max_retries} retries: {last_exc}")
        raise FetchError(f"Giving up on {url} after {self.settings.max_retries} retries: {last_exc}")

    def _cool_off(self, host: str, asked_wait: float = 0.0) -> None:
        """A host that refused every retry with HTTP 429 (or refused its robots.txt) is left alone for a while.

        Doubling, not fixed: Workable refused every request for four days (23-26 Sep 2026), and a fixed 15 minutes
        meant four stalls an hour spent being refused again. A host that keeps saying no is asked less and less
        often -- 15 min, 30, 1 h ... 6 h at most -- never sooner than it asked (Retry-After), and one successful
        answer resets it. Several hosts of one provider group refusing cools the whole provider."""
        with self._rate_guard:
            self.longest_retry_after = max(self.longest_retry_after, float(asked_wait or 0))
        n = self._refusals.get(host, 0) + 1
        self._refusals[host] = n
        cooldown = min(self.settings.rate_limit_cooldown_seconds * 2 ** (n - 1), 6 * 3600)
        cooldown = max(cooldown, float(asked_wait or 0))          # never back sooner than the host asked
        self._cooling_until[host] = time.monotonic() + cooldown
        log.warning("%s refused every retry with HTTP 429 -- leaving it alone for %.0f min", host, cooldown / 60)
        group = self.group_of(host)
        if group:
            with self._rate_guard:
                refused = self._group_refused_hosts.setdefault(group, set())
                refused.add(host)
                if len(refused) >= max(1, self.settings.provider_trip_hosts):
                    g = self._group_refusals.get(group, 0) + 1
                    self._group_refusals[group] = g
                    pause = min(self.settings.rate_limit_cooldown_seconds * 2 ** (g - 1), 6 * 3600)
                    self._group_cooling_until[group] = time.monotonic() + max(pause, float(asked_wait or 0))
                    refused.clear()
                    log.warning("%s: %d of its hosts refused every retry -- leaving the whole provider "
                                "alone for %.0f min", group, self.settings.provider_trip_hosts, pause / 60)

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


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
