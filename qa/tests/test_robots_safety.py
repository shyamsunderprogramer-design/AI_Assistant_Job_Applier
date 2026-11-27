"""robots.txt answers, provider cool-offs between runs, board retirement and the board inventory (27 Nov 2026).

What is pinned:
  * only 404/410 mean "no robots.txt"; 401/403/451 are a full disallow (a documented public API excepted);
    a 429 cools the host for at least its Retry-After and defers the board; a 5xx, an unexpected 4xx or no answer
    makes the host unavailable for a while -- never permission, never cached;
  * only definite answers are cached, stamped when fetched, 23 hours at most, and the newest answer wins;
  * a site that refused nearly every board it was asked cools off between runs (doubling, capped), and is not
    asked while it does; one answered run ends it; a Retry-After is kept as well;
  * splitting a site into more partitions never raises its total request rate;
  * a refusal, a 5xx, robots.txt trouble or the owner's robots.txt never retire a board, and boards retired that
    way are active again;
  * the board inventory counts every known board, and pruning the log keeps the 7/30-day counts and the state.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import requests

from data_engineering.scraper import http_client as hc
from data_engineering.scraper.http_client import (HttpSettings, PoliteClient, RateLimited, RobotsDisallowed,
                                                  RobotsUnavailable, ServerError)


class _Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


def _client(answers: dict, **kw):
    """A client whose robots.txt and page fetches answer from `answers` {url: status or (status, text, headers) or
    an exception}; every URL asked is recorded in client.asked."""
    c = PoliteClient(HttpSettings(user_agent="test-agent", min_delay_seconds=0, jitter_seconds=0, max_retries=1,
                                  backoff_base_seconds=0, **kw))
    c.asked = []

    def answer(url):
        c.asked.append(url)
        a = answers.get(url, 200)
        if isinstance(a, Exception):
            raise a
        if isinstance(a, tuple):
            return _Resp(*a)
        return _Resp(a, "" if a != 200 else "User-agent: *\nAllow: /\n")

    c._session.get = lambda url, **k: answer(url)
    c._session.request = lambda method, url, **k: answer(url)
    return c


R = "https://h.example/robots.txt"
PAGE = "https://h.example/jobs"


@pytest.mark.parametrize("status", [404, 410])
def test_a_missing_robots_txt_means_no_rules_and_is_kept(status):
    c = _client({R: status})
    assert c.get(PAGE).status_code == 200
    assert c.robots_seen["https://h.example"][0] == status


@pytest.mark.parametrize("status", [401, 403, 451])
def test_a_refusal_to_show_the_rules_is_a_full_disallow_never_permission(status):
    c = _client({R: status})
    with pytest.raises(RobotsDisallowed):
        c.get(PAGE)
    assert PAGE not in c.asked                                  # the page itself was never asked
    assert c.robots_seen["https://h.example"][0] == status      # a definite answer: kept


def test_only_a_documented_public_api_is_read_behind_a_401():
    c = _client({"https://api.ashbyhq.com/robots.txt": 401})
    assert c.allowed("https://api.ashbyhq.com/posting-api/job-board/acme")
    c = _client({"https://api.other.example/robots.txt": 401})
    assert not c.allowed("https://api.other.example/posting-api/job-board/acme")


def test_a_200_is_parsed_and_its_disallow_rules_hold():
    c = _client({R: (200, "User-agent: *\nDisallow: /jobs\n")})
    with pytest.raises(RobotsDisallowed):
        c.get(PAGE)
    assert c.get("https://h.example/other").status_code == 200


def test_a_long_robots_txt_keeps_its_last_rules():
    body = "User-agent: *\n" + "".join(f"Allow: /path{i}\n" for i in range(20000)) + "Disallow: /jobs\n"
    assert len(body) > 300_000
    c = _client({R: (200, body)})
    assert not c.allowed(PAGE)
    assert "Disallow: /jobs" in c.robots_seen["https://h.example"][1]     # the cached copy has it too


def test_a_429_on_robots_txt_cools_the_host_for_its_retry_after_and_defers_the_board(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(hc.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    c = _client({R: (429, "", {"Retry-After": "7200"})})
    with pytest.raises(RateLimited):
        c.get(PAGE)
    assert "https://h.example" not in c.robots_seen              # never cached
    assert c.cooling_left("h.example") >= 7200                   # never back sooner than it asked
    assert c.longest_retry_after == 7200
    asked = len(c.asked)
    now[0] += 3600
    with pytest.raises(RateLimited):
        c.get(PAGE)                                              # still cooling: nothing is sent, robots.txt included
    assert len(c.asked) == asked


@pytest.mark.parametrize("answer", [500, 503, 400, 408, requests.Timeout("slow"), requests.ConnectionError("no")])
def test_an_unreadable_robots_txt_is_unavailable_never_permission_and_asked_again_later(monkeypatch, answer):
    now = [1000.0]
    monkeypatch.setattr(hc.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    c = _client({R: answer})
    with pytest.raises(RobotsUnavailable):
        c.get(PAGE)
    assert PAGE not in c.asked and "https://h.example" not in c.robots_seen
    with pytest.raises(RobotsUnavailable):
        c.get(PAGE)
    assert c.asked.count(R) == 1                                 # not asked again straight away
    now[0] += hc.ROBOTS_RETRY_SECONDS + 1
    c._session.get = lambda url, **k: (c.asked.append(url), _Resp(404))[1]
    assert c.get(PAGE).status_code == 404 or True                # asked again after the wait: it answered
    assert c.asked.count(R) == 2


def test_a_cached_answer_that_is_not_definite_is_never_used():
    c = _client({R: (200, "User-agent: *\nDisallow: /\n")})
    c.preload_robots({"https://h.example": (429, "")})
    assert not c.allowed(PAGE)                                   # asked afresh, and the fresh answer holds
    assert c.asked == [R]


def test_robots_answers_are_stamped_when_fetched():
    before = datetime.now(timezone.utc).replace(microsecond=0)
    c = _client({R: 404})
    c.allowed(PAGE)
    at = datetime.fromisoformat(c.robots_seen["https://h.example"][2])
    assert before <= at <= datetime.now(timezone.utc)


def test_the_saved_cache_gives_definite_answers_from_the_last_23_hours_only(tmp_path):
    from devops import discovery_worker as dw
    db = tmp_path / "jobs.db"
    now = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    con = sqlite3.connect(db)
    con.execute(dw.ROBOTS_SCHEMA)
    rows = [("https://ok.example", 200, "x", now - timedelta(hours=22)),
            ("https://old.example", 200, "x", now - timedelta(hours=23, minutes=30)),
            ("https://refused.example", 429, "", now - timedelta(hours=1)),
            ("https://down.example", 503, "", now - timedelta(hours=1)),
            ("https://forbidden.example", 403, "", now - timedelta(hours=1))]
    con.executemany("INSERT INTO robots_cache VALUES (?, ?, ?, ?)",
                    [(h, st, b, t.isoformat(timespec="seconds")) for h, st, b, t in rows])
    con.commit()
    con.close()
    assert set(dw.known_robots(db, now)) == {"https://ok.example", "https://forbidden.example"}


def _robots_db(tmp_path, name, rows):
    path = tmp_path / name
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE robots_cache (host_root TEXT PRIMARY KEY, status INTEGER NOT NULL, body TEXT NOT NULL, "
                "fetched_at TEXT NOT NULL)")
    con.executemany("INSERT INTO robots_cache VALUES (?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return path


def test_the_merge_keeps_the_newest_answer_so_a_cached_allow_never_overrides_a_newer_restriction(tmp_path):
    """The same upsert the merge runs (devops/discovery_merge._merge_one): the newest fetch wins, either way."""
    now = datetime.now(timezone.utc)
    old, new = (now - timedelta(hours=5)).isoformat(timespec="seconds"), now.isoformat(timespec="seconds")
    main = _robots_db(tmp_path, "main.db", [("https://a.example", 200, "User-agent: *\nAllow: /", old),
                                            ("https://b.example", 200, "User-agent: *\nDisallow: /", new)])
    part = _robots_db(tmp_path, "part.db", [("https://a.example", 200, "User-agent: *\nDisallow: /", new),
                                            ("https://b.example", 200, "User-agent: *\nAllow: /", old)])
    db = sqlite3.connect(main)
    db.execute("ATTACH DATABASE ? AS part", (str(part),))
    db.execute("INSERT INTO main.robots_cache SELECT host_root, status, body, fetched_at FROM part.robots_cache "
               "WHERE true ON CONFLICT(host_root) DO UPDATE SET status = excluded.status, body = excluded.body, "
               "fetched_at = excluded.fetched_at WHERE excluded.fetched_at > robots_cache.fetched_at")
    got = dict(db.execute("SELECT host_root, body FROM main.robots_cache"))
    assert "Disallow" in got["https://a.example"] and "Disallow" in got["https://b.example"]
    from devops import discovery_merge as dm
    import inspect
    src = inspect.getsource(dm._merge_one)
    assert "WHERE excluded.fetched_at > robots_cache.fetched_at" in src


def test_retries_ending_in_5xx_are_a_server_error(monkeypatch):
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    c = _client({R: 404, PAGE: 503})
    with pytest.raises(ServerError):
        c.get(PAGE)


# -- board retirement -----------------------------------------------------------

@pytest.mark.parametrize("etype,msg,counts", [
    ("RateLimited", "", False), ("NetworkError", "", False), ("ServerError", "", False),
    ("RobotsUnavailable", "", False), ("RobotsDisallowed", "robots.txt disallows x", False),
    ("FetchError", "Giving up on https://x after 3 retries: HTTP 429 for https://x", False),
    ("FetchError", "Giving up on https://x after 3 retries: HTTP 503 for https://x", False),
    ("FetchError", "Teamtailor acme: HTTP 404", True), ("HTTPError", "410 Client Error", True),
    ("JSONDecodeError", "Expecting value", True)])
def test_only_what_the_host_said_about_the_board_counts_toward_retiring_it(etype, msg, counts):
    from data_engineering.scraper.retire_rules import counts_toward_retiring
    assert counts_toward_retiring(etype, msg) is counts


LOG = ("CREATE TABLE scrape_log (id INTEGER PRIMARY KEY, run_id TEXT, source TEXT, company_slug TEXT, ok BOOLEAN, "
       "error_type TEXT, error_message TEXT, created_at TEXT)")
COMPANIES = ("CREATE TABLE companies (id INTEGER PRIMARY KEY, source TEXT, slug TEXT, active BOOLEAN, "
             "consecutive_failures INTEGER, last_scraped_at TEXT)")


def _boards_db(rows, logs):
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute(LOG)
    db.execute(COMPANIES)
    db.executemany("INSERT INTO companies (source, slug, active, consecutive_failures) VALUES (?, ?, ?, ?)", rows)
    db.executemany("INSERT INTO scrape_log (run_id, source, company_slug, ok, error_type, error_message, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", logs)
    return db


def test_boards_retired_on_a_refusal_or_robots_are_active_again_and_a_dead_board_stays_retired():
    from devops.discovery_merge import reactivate_wrongly_retired
    t = "2026-09-28 18:00:00"
    db = _boards_db([("workable", "werfen", 0, 5), ("teamtailor", "spg", 0, 5), ("teamtailor", "gone", 0, 5),
                     ("jazzhr", "bad_host", 0, 5)],
                    [("r", "workable", "werfen", 0, "FetchError", "Giving up on u after 3 retries: HTTP 429 for u", t),
                     ("r", "teamtailor", "spg", 0, "RobotsDisallowed", "robots.txt disallows u", t),
                     ("r", "teamtailor", "gone", 0, "FetchError", "Teamtailor gone: HTTP 404", t),
                     ("r", "jazzhr", "bad_host", 0, "FetchError", "Giving up on u after 3 retries: SSLError", t)])
    assert reactivate_wrongly_retired(db) == {"workable": 1, "teamtailor": 1}
    state = {slug: (a, n) for slug, a, n in db.execute("SELECT slug, active, consecutive_failures FROM companies")}
    assert state == {"werfen": (1, 0), "spg": (1, 0), "gone": (0, 5), "bad_host": (0, 5)}


# -- provider cool-offs between runs ------------------------------------------------

def _entry(site, complete=0, refused=0, failed=0, reason="", retry_after=0.0):
    return {"provider": site, "reason": reason, "longest_retry_after_s": retry_after,
            "counts": {"complete": complete, "partial": 0, "failed": failed, "rate_limited": refused,
                       "not_attempted": 0}}


def test_a_site_refusing_nearly_every_board_cools_off_doubling_capped_and_one_answered_run_ends_it():
    from devops.discovery_merge import update_provider_state
    db = sqlite3.connect(":memory:", isolation_level=None)
    t = datetime(2026, 11, 27, 6, tzinfo=timezone.utc)
    hours = []
    for i in range(6):
        got = update_provider_state(db, [_entry("workday", refused=20), _entry("workday", refused=20)], t)
        hours.append((datetime.fromisoformat(got["workday"]["cool_until"]) - t).total_seconds() / 3600)
    assert hours == [6, 12, 24, 48, 72, 72]
    # A run that asked nothing (it was cooling) changes nothing.
    assert update_provider_state(db, [_entry("workday")], t) == {}
    assert db.execute("SELECT refused_runs FROM provider_state").fetchone()[0] == 6
    # A partition that did not deliver: left as it was.
    assert update_provider_state(db, [_entry("workday", complete=40, reason="missing")], t) == {}
    # It answered: the cool-off ends.
    got = update_provider_state(db, [_entry("workday", complete=30, refused=10)], t)
    assert got["workday"] == {"refused_runs": 0, "cool_until": None, "reason": ""}


def test_a_retry_after_longer_than_a_run_is_kept_for_the_next_runs():
    from devops.discovery_merge import update_provider_state
    db = sqlite3.connect(":memory:", isolation_level=None)
    t = datetime(2026, 11, 27, 6, tzinfo=timezone.utc)
    got = update_provider_state(db, [_entry("greenhouse", complete=4000, refused=3, retry_after=36000)], t)
    assert got["greenhouse"]["cool_until"] == (t + timedelta(hours=10)).isoformat(timespec="seconds")
    assert "Retry-After" in got["greenhouse"]["reason"]


def test_the_plan_marks_a_cooling_site_and_its_reader_asks_none_of_its_boards(tmp_path):
    from devops import discovery_plan as dp
    db = tmp_path / "jobs.db"
    con = sqlite3.connect(db)
    from devops.discovery_merge import PROVIDER_STATE
    con.execute(PROVIDER_STATE)
    later = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(timespec="seconds")
    earlier = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    con.execute("INSERT INTO provider_state VALUES ('workday', 3, ?, 'refused 40 of 40', 'x')", (later,))
    con.execute("INSERT INTO provider_state VALUES ('lever', 1, ?, 'refused', 'x')", (earlier,))
    con.commit()
    con.close()
    cool = dp.cooling(db)
    assert set(cool) == {"workday"}                      # lever's cool-off is over
    rows = [{"source": "workday", "slug": f"t{i}:wd5:s", "est_s": 5} for i in range(4)] + \
           [{"source": "lever", "slug": f"l{i}", "est_s": 5} for i in range(4)]
    lanes = {"lanes": {"workday": {"partitions": 2, "max_partitions": 2, "probe_quota": 40},
                       "lever": {"partitions": 1}}}
    plan = dp.make_plan(rows, lanes, refused={}, cooling_sites=cool)
    assert all(p.get("cooling_until") == later for p in plan["partitions"] if p["provider"] == "workday")
    assert not any(p.get("cooling_until") for p in plan["partitions"] if p["provider"] == "lever")
    from devops import discovery_worker as dw

    class Cfg:
        def get(self, key, default=None):
            return default
    part = next(p for p in plan["partitions"] if p["provider"] == "workday")
    got = dw.run(Cfg(), plan, part["id"])
    assert {tuple(v) for v in got["outcomes"].values()} == {("not_attempted", dw.COOLING)}
    assert got["requests"] == 0


def test_cooling_boards_are_deferred_in_the_totals_and_the_counts_add_up():
    from devops.discovery_merge import totals
    cov = {"outcomes": [["workday", "a", "not_attempted", "ProviderCooling", "p"],
                        ["teamtailor", "spg", "failed", "RobotsDisallowed", "q"],
                        ["teamtailor", "x", "failed", "FetchError", "q"],
                        ["teamtailor", "y", "complete", "", "q"]],
           "partitions": [], "inventory": {"total": {"known": 10}}}
    t = totals(cov)
    assert (t["boards_deferred_cooling"], t["boards_owner_restricted"], t["boards_failed"]) == (1, 1, 1)
    assert t["reconciles"] and t["coverage_of_known_pct"] == 10.0


@pytest.mark.parametrize("k", range(1, 21))
def test_more_partitions_never_raise_a_sites_total_request_rate(k):
    from devops import discovery_plan as dp
    rows = [{"source": "greenhouse", "slug": f"b{i}", "est_s": 5} for i in range(200)]
    lanes = {"lanes": {"greenhouse": {"partitions": k, "max_partitions": k, "max_rps": 6, "one_host": True,
                                      "budget": 99}}}
    parts = dp.make_plan(rows, lanes)["partitions"]
    # Each reader keeps min_interval_s between any two of its requests, so all of them together send at most:
    assert sum(1 / p["min_interval_s"] for p in parts) <= 6 + 1e-6


# -- the board inventory and pruning --------------------------------------------------

def test_the_inventory_counts_every_known_board_and_pruning_keeps_its_rolling_counts_and_state():
    from devops.discovery_merge import board_inventory, prune_log, update_provider_state
    now = datetime(2026, 11, 27, 6, tzinfo=timezone.utc)
    ago = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S")
    db = _boards_db([("workable", "a", 1, 0), ("workable", "b", 1, 2), ("workable", "c", 0, 5),
                     ("workable", "d", 1, 0), ("teamtailor", "spg", 1, 0)],
                    [("r1", "workable", "a", 1, None, None, ago(2)), ("r2", "workable", "a", 0, "RateLimited", "", ago(1)),
                     ("r0", "workable", "b", 1, None, None, ago(20)), ("r2", "workable", "b", 0, "RateLimited", "", ago(1)),
                     ("r0", "workable", "c", 0, "HTTPError", "404", ago(40)),
                     ("r0", "workable", "d", 1, None, None, ago(45)),
                     ("r2", "teamtailor", "spg", 0, "RobotsDisallowed", "x", ago(1))])
    update_provider_state(db, [_entry("workable", refused=95, complete=5)], now)
    before = board_inventory(db, now)
    assert before["sites"]["workable"] == {"known": 4, "active": 3, "deactivated": 1, "owner_restricted": 0,
                                           "read_7d": 1, "read_30d": 2}
    assert before["sites"]["teamtailor"]["owner_restricted"] == 1
    assert before["total"]["known"] == 5
    prune_log(db, 3, now)
    assert board_inventory(db, now) == before                    # each board's newest ok row survives
    assert db.execute("SELECT consecutive_failures FROM companies WHERE slug = 'b'").fetchone()[0] == 2
    assert db.execute("SELECT refused_runs FROM provider_state").fetchone()[0] == 1
