"""Polite HTTP client: robots.txt enforcement, per-host rate limiting, backoff.

See README.md §C5. Do not weaken these defaults to go faster.
"""

from __future__ import annotations

import logging
import random
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)


class RobotsDisallowed(Exception):
    """Raised when robots.txt forbids a URL. Caller logs and skips."""


class FetchError(Exception):
    """Non-retryable or retry-exhausted HTTP failure."""


@dataclass
class HttpSettings:
    user_agent: str
    respect_robots: bool = True
    timeout_seconds: int = 20
    min_delay_seconds: float = 1.5
    jitter_seconds: float = 0.75
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    # Treat a 401/403 on robots.txt as a blanket disallow. Stricter than
    # RFC 9309, which says a 4xx means the file is unavailable and the crawler
    # may proceed. Off by default; see _robots_for().
    strict_robots_on_4xx: bool = False

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
            strict_robots_on_4xx=bool(cfg.get("http.strict_robots_on_4xx", False)),
        )


class PoliteClient:
    """Wraps requests with per-host pacing and robots.txt checks."""

    def __init__(self, settings: HttpSettings):
        self.settings = settings
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": settings.user_agent})
        self._last_request_at: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # -- robots ------------------------------------------------------------
    def _robots_for(self, host_root: str) -> urllib.robotparser.RobotFileParser | None:
        """Fetch and cache robots.txt for a scheme://host. None = unavailable."""
        if host_root in self._robots:
            return self._robots[host_root]

        parser = urllib.robotparser.RobotFileParser()
        robots_url = f"{host_root}/robots.txt"
        try:
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
        delay = self.settings.min_delay_seconds + random.uniform(0, self.settings.jitter_seconds)
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < delay:
                time.sleep(delay - elapsed)
        self._last_request_at[host] = time.monotonic()

    # -- requests ----------------------------------------------------------
    def get(self, url: str, **kwargs) -> requests.Response:
        """GET with robots check, pacing, and bounded backoff on 429/5xx."""
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        host = urlparse(url).netloc
        last_exc: Exception | None = None

        for attempt in range(self.settings.max_retries + 1):
            self._wait_turn(host)
            try:
                resp = self._session.get(
                    url, timeout=self.settings.timeout_seconds, **kwargs
                )
            except requests.RequestException as exc:
                last_exc = exc
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = FetchError(f"HTTP {resp.status_code} for {url}")
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        time.sleep(min(int(retry_after), 60))
                        continue
                else:
                    return resp

            if attempt < self.settings.max_retries:
                backoff = self.settings.backoff_base_seconds ** (attempt + 1)
                log.debug("Retry %d for %s in %.1fs (%s)", attempt + 1, url, backoff, last_exc)
                time.sleep(backoff)

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
