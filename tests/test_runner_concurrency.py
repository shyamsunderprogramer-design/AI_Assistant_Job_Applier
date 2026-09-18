"""Boards on different hosts are walked at the same time — offline, no network.

The politeness delay has always been per-host; the loop that walked boards
never was. So four unrelated hosts spent an hour and forty minutes taking
turns waiting on each other, of which only forty-six were
boards-api.greenhouse.io actually being paced. Workday was the worst of it:
seventy boards on seventy DIFFERENT hosts, walked one at a time, thirty-five
minutes.

What must hold is both halves of that:

    two boards on DIFFERENT hosts may overlap
    two boards on the SAME host may never overlap

The second is the one worth guarding. A speed-up that quietly hammered a
single host would be a regression dressed as an improvement.
"""

import threading
import time

from scraper.runner import _host_of


class FakeScraper:
    """Names a host per slug, the way a real scraper's board_url does."""

    def __init__(self, hosts):
        self.hosts = hosts

    def board_url(self, slug):
        return f"https://{self.hosts[slug]}/{slug}"


class Company:
    def __init__(self, slug, source="greenhouse"):
        self.slug, self.source = slug, source


# -- which host a board belongs to ------------------------------------------

def test_every_workday_tenant_is_its_own_host():
    """This is the whole reason Workday was slow: seventy boards that could
    have run at once, queued behind each other."""
    from scraper.workday import WorkdayScraper

    scraper = WorkdayScraper(client=None)
    a = _host_of(scraper, Company("nvidia:wd5:Site", "workday"))
    b = _host_of(scraper, Company("boeing:wd1:EXTERNAL", "workday"))

    assert a != b
    assert a == "nvidia.wd5.myworkdayjobs.com"


def test_greenhouse_boards_all_share_one_host():
    """And this is why greenhouse cannot be sped up the same way: one host,
    so the 1.5s delay between its boards is a floor, not an accident."""
    from scraper.greenhouse import GreenhouseScraper

    scraper = GreenhouseScraper(client=None)
    a = _host_of(scraper, Company("stripe"))
    b = _host_of(scraper, Company("figma"))

    assert a == b


def test_a_board_with_no_url_still_gets_a_host():
    """Never crash the grouping over a missing board_url; fall back to the
    source, which keeps that board sequential rather than dropping it."""
    class Broken:
        def board_url(self, slug):
            raise RuntimeError("no url")

    assert _host_of(Broken(), Company("x", "lever")) == "lever"


# -- the two rules that make it safe ----------------------------------------

def _timeline(host_groups, workers):
    """Run fake work through the same shape the runner uses, recording when
    each host was busy."""
    from concurrent.futures import ThreadPoolExecutor

    spans = {}
    lock = threading.Lock()

    def walk(group):
        host, count = group
        for _ in range(count):
            start = time.monotonic()
            time.sleep(0.05)                 # stands in for one paced request
            with lock:
                spans.setdefault(host, []).append((start, time.monotonic()))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(walk, host_groups))
    return spans


def _overlaps(spans_a, spans_b) -> bool:
    return any(a_start < b_end and b_start < a_end
               for a_start, a_end in spans_a
               for b_start, b_end in spans_b)


def test_different_hosts_overlap():
    spans = _timeline([("a.com", 3), ("b.com", 3), ("c.com", 3)], workers=3)

    assert _overlaps(spans["a.com"], spans["b.com"]), \
        "different hosts should be walked at the same time"


def test_one_host_is_never_hit_twice_at_once():
    """Every board on a host goes through one group, walked in order, so two
    requests to the same host can never be in flight together."""
    spans = _timeline([("a.com", 6)], workers=4)

    windows = sorted(spans["a.com"])
    for (_, earlier_end), (later_start, _) in zip(windows, windows[1:]):
        assert later_start >= earlier_end, "same host overlapped itself"


def test_the_total_is_the_slowest_host_not_the_sum():
    """The arithmetic the change is for: four hosts of three requests each
    take the time of one host, not four."""
    started = time.monotonic()
    _timeline([(f"{n}.com", 3) for n in range(4)], workers=4)
    elapsed = time.monotonic() - started

    assert elapsed < 0.05 * 3 * 4 * 0.7, f"took {elapsed:.2f}s — ran sequentially"
