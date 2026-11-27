"""A site's request-rate cap across all its readers, and in-run retries measured (26 Nov 2026). Offline."""

from __future__ import annotations

import time

from data_engineering.scraper import runner
from data_engineering.scraper.http_client import HttpSettings, PoliteClient
from devops import discovery_plan as dp
from devops import discovery_worker as dw
from qa.tests.test_discovery_partitions import (  # noqa: F401 -- `offline`: the autouse no-network fixture
    BASE_BOARDS, CALLS, WORLD, Cfg, healthy_world, offline, plan_for, run_partition, seed)


def test_the_site_wide_interval_spaces_requests_whatever_their_hosts():
    c = PoliteClient(HttpSettings(user_agent="test", min_delay_seconds=0.0, jitter_seconds=0.0, min_interval_seconds=0.2,
                                  respect_robots=False))
    t0 = time.monotonic()
    for host in ("a.example", "b.example", "c.example"):
        c._wait_turn(host)                      # three different hosts: no per-host gap applies
    assert time.monotonic() - t0 >= 0.39         # two intervals of 0.2 between three requests


def test_the_plan_gives_each_reader_its_share_of_the_sites_cap():
    rows = [{"source": "greenhouse", "slug": f"g{i}", "name": "", "board_url": "", "origin": "", "est_s": 2.0}
            for i in range(90)]
    lanes = {"lanes": {"greenhouse": {"partitions": 9, "max_partitions": 9, "budget": 9, "one_host": True,
                                      "max_rps": 5.0},
                       "workday": {"in_run_retry": False}}}
    plan = dp.make_plan(rows + [{"source": "workday", "slug": "w1", "name": "", "board_url": "", "origin": "",
                                 "est_s": 2.0}], lanes)
    gh = [p for p in plan["partitions"] if p["provider"] == "greenhouse"]
    assert len(gh) == 9 and all(p["min_interval_s"] == 1.8 for p in gh)          # 9 readers x 1/1.8 s = 5 a second
    wd = [p for p in plan["partitions"] if p["provider"] == "workday"]
    assert wd and all(p["in_run_retry"] is False for p in wd)


def test_refused_boards_are_counted_and_recovered_ones_too(tmp_path, monkeypatch):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    WORLD.update({w: 429 for w in ("w1", "w2", "w3", "w4", "w5")})
    plan = plan_for(base)
    pid = next(p["id"] for p in plan["partitions"] if p["provider"] == "workday")
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)                 # the cool-off wait, not slept here
    part = run_partition(tmp_path, base, plan, pid, cfg=Cfg(**{"http.rate_limit_cooldown_seconds": 1}))
    import json
    rep = json.loads((part.parent / "report.json").read_text())
    r = rep["retry"]
    assert r["deferred"] >= 1 and r["recovered"] == 0 and r["still_refused"] == r["deferred"]
    assert rep["requests"] >= r["deferred"]


def test_with_no_in_run_retry_refused_boards_stay_for_the_next_run_and_no_time_is_waited(tmp_path, monkeypatch):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    WORLD.update({w: 429 for w in ("w1", "w2", "w3", "w4", "w5")})
    plan = plan_for(base)
    for p in plan["partitions"]:
        if p["provider"] == "workday":
            p["in_run_retry"] = False
    pid = next(p["id"] for p in plan["partitions"] if p["provider"] == "workday")
    slept = []
    monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))
    import json
    part = run_partition(tmp_path, base, plan, pid)
    rep = json.loads((part.parent / "report.json").read_text())
    assert rep["retry"]["recovered"] == 0 and rep["retry"]["waited_s"] == 0 and not [s for s in slept if s > 60]
    assert rep["counts"]["rate_limited"] >= 1 and rep["counts"]["complete"] == 0   # never called read
    # Not excluded: they are the least recently read, so the next plan asks them first.
    _ = dw


def test_a_robots_fetch_is_paced_and_counted_like_any_request(monkeypatch):
    from types import SimpleNamespace
    c = PoliteClient(HttpSettings(user_agent="test", min_delay_seconds=0.0, jitter_seconds=0.0, min_interval_seconds=0.15,
                                  respect_robots=True))
    monkeypatch.setattr(c._session, "get", lambda url, **kw: SimpleNamespace(status_code=404, text=""))
    t0 = time.monotonic()
    c._robots_for("https://a.example")
    c._robots_for("https://b.example")
    assert c.requests_made == 2 and time.monotonic() - t0 >= 0.14     # the site-wide interval applied between them


def test_every_site_has_a_request_rate_cap():
    import json
    from pathlib import Path
    lanes = json.loads((Path(__file__).resolve().parents[2] / "devops" / "discovery_lanes.json").read_text())["lanes"]
    assert all(float(lane.get("max_rps") or 0) > 0 for lane in lanes.values()), \
        [s for s, lane in lanes.items() if not lane.get("max_rps")]


def test_a_robots_fetch_does_not_hold_back_the_page_after_it_on_the_same_host(monkeypatch):
    from types import SimpleNamespace
    c = PoliteClient(HttpSettings(user_agent="test", min_delay_seconds=0.4, jitter_seconds=0.0, respect_robots=True))
    monkeypatch.setattr(c._session, "get", lambda url, **kw: SimpleNamespace(status_code=404, text=""))
    t0 = time.monotonic()
    c._robots_for("https://a.example")
    c._wait_turn("a.example")                       # the board's first page: no per-host gap after robots.txt
    assert time.monotonic() - t0 < 0.3
    c._wait_turn("a.example")                       # its second request: the per-host gap, as always
    assert time.monotonic() - t0 >= 0.39


def test_a_robots_answer_from_the_last_day_is_used_and_only_definite_answers_are_kept(monkeypatch):
    from types import SimpleNamespace
    c = PoliteClient(HttpSettings(user_agent="test", min_delay_seconds=0.0, jitter_seconds=0.0, respect_robots=True))
    answers = {"https://a.example/robots.txt": 200, "https://b.example/robots.txt": 404, "https://c.example/robots.txt": 503}
    monkeypatch.setattr(c._session, "get", lambda url, **kw: SimpleNamespace(
        status_code=answers[url], text="User-agent: *\nDisallow: /private" if answers[url] == 200 else ""))
    c.preload_robots({"https://k.example": (200, "User-agent: *\nDisallow: /")})
    assert not c.allowed("https://k.example/jobs") and c.requests_made == 0       # from the cache: no request
    for host in ("a", "b", "c"):
        c._robots_for(f"https://{host}.example")
    assert set(c.robots_seen) == {"https://a.example", "https://b.example"}      # a 5xx is never kept
    assert c.allowed("https://a.example/jobs") and not c.allowed("https://a.example/private/x")


def test_robots_answers_older_than_a_day_are_not_used_and_the_join_keeps_the_newest(tmp_path):
    import sqlite3
    from datetime import datetime, timedelta, timezone
    db = tmp_path / "saved.db"
    con = sqlite3.connect(db)
    con.execute(dw.ROBOTS_SCHEMA)
    now = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    con.executemany("INSERT INTO robots_cache VALUES (?, ?, ?, ?)", [
        ("https://new.example", 200, "x", (now - timedelta(hours=3)).isoformat(timespec="seconds")),
        ("https://old.example", 200, "y", (now - timedelta(hours=30)).isoformat(timespec="seconds"))])
    con.commit()
    con.close()
    assert set(dw.known_robots(db, now)) == {"https://new.example"}
    assert dw.known_robots(tmp_path / "missing.db", now) == {}
