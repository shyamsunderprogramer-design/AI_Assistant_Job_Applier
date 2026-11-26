"""Cloud-only discovery (25 Nov 2026): the two windows a day, every site in the cloud, the job-search sites merged."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from devops import discovery_feeds as dfeeds
from devops import discovery_merge as dm
from devops import discovery_plan as dp
from devops import discovery_result as dr
from devops import discovery_slot as ds

UTC = timezone.utc


# -- the windows: 1:00 and 11:00 Central, daylight saving followed ------------------------------------------------

@pytest.mark.parametrize("now, start", [
    (datetime(2026, 1, 15, 7, 0, tzinfo=UTC), datetime(2026, 1, 15, 7, 0, tzinfo=UTC)),     # 1:00 CST
    (datetime(2026, 7, 15, 6, 0, tzinfo=UTC), datetime(2026, 7, 15, 6, 0, tzinfo=UTC)),     # 1:00 CDT
    (datetime(2026, 1, 15, 17, 5, tzinfo=UTC), datetime(2026, 1, 15, 17, 0, tzinfo=UTC)),   # 11:05 CST
    (datetime(2026, 7, 15, 16, 0, tzinfo=UTC), datetime(2026, 7, 15, 16, 0, tzinfo=UTC)),   # 11:00 CDT
    (datetime(2026, 1, 15, 6, 55, tzinfo=UTC), datetime(2026, 1, 15, 7, 0, tzinfo=UTC)),    # 5 min early: the coming one
    (datetime(2026, 1, 15, 3, 0, tzinfo=UTC), datetime(2026, 1, 14, 17, 0, tzinfo=UTC)),    # 21:00 CST: the 11:00 one
    (datetime(2026, 3, 8, 6, 0, tzinfo=UTC), datetime(2026, 3, 7, 17, 0, tzinfo=UTC)),      # 00:00 CST, DST day
    (datetime(2026, 3, 8, 7, 0, tzinfo=UTC), datetime(2026, 3, 8, 7, 0, tzinfo=UTC)),       # 1:00 CST, DST day
])
def test_a_run_belongs_to_the_latest_central_start_whatever_the_utc_offset(now, start):
    assert ds.slot_start(now) == start


def test_once_per_window_but_the_button_always_runs():
    start = datetime(2026, 11, 26, 7, 0, tzinfo=UTC)          # 1:00 CST
    later = datetime(2026, 11, 26, 13, 30, tzinfo=UTC)        # GitHub's schedule, hours late, same window
    read = [{"id": 1, "created": start, "scraped": True}]
    assert ds.decide("workflow_dispatch", "scheduler", start, [])["go"]
    assert not ds.decide("schedule", "", later, read)["go"]
    assert not ds.decide("workflow_dispatch", "scheduler", later, read)["go"]
    assert ds.decide("workflow_dispatch", "manual", later, read)["go"]
    assert ds.decide("workflow_dispatch", "", later, read)["go"]
    # A run that stopped at the check (nothing scraped), or the window before, does not count.
    assert ds.decide("schedule", "", later, [{"id": 2, "created": start, "scraped": False}])["go"]
    yesterday = [{"id": 3, "created": datetime(2026, 11, 25, 17, 0, tzinfo=UTC), "scraped": True}]
    assert ds.decide("schedule", "", later, yesterday)["go"]


def test_a_late_start_close_to_the_next_window_leaves_the_reading_to_it():
    # GitHub's 06:23 UTC line in winter is 00:23 CST: inside the 11:00 window, 37 min before the 1:00 one.
    assert not ds.decide("schedule", "", datetime(2026, 11, 26, 6, 23, tzinfo=UTC), [])["go"]
    # Its 16:23 UTC line in winter is 10:23 CST: the 1:00 window, 37 min before 11:00.
    assert not ds.decide("schedule", "", datetime(2026, 11, 26, 16, 23, tzinfo=UTC), [])["go"]
    # The lines for the season in force: inside their own window, hours before the next.
    assert ds.decide("schedule", "", datetime(2026, 11, 26, 7, 23, tzinfo=UTC), [])["go"]        # 1:23 CST
    assert ds.decide("schedule", "", datetime(2026, 7, 15, 6, 23, tzinfo=UTC), [])["go"]         # 1:23 CDT
    assert ds.decide("schedule", "", datetime(2026, 7, 15, 16, 23, tzinfo=UTC), [])["go"]        # 11:23 CDT
    # The outside scheduler at the minute is never near the next window; the button always runs.
    assert ds.decide("workflow_dispatch", "scheduler", datetime(2026, 11, 26, 7, 0, tzinfo=UTC), [])["go"]
    assert ds.decide("workflow_dispatch", "manual", datetime(2026, 11, 26, 6, 23, tzinfo=UTC), [])["go"]


# -- every site in the cloud; a provider that refused its last run is only probed -------------------------------

def _rows(**sites):
    return [{"source": s, "slug": f"{s}{i}", "name": "", "board_url": "", "origin": "", "est_s": 2.0}
            for s, n in sites.items() for i in range(n)]


def test_workday_is_read_in_the_cloud_and_only_probed_while_it_refuses():
    lanes = dp.load_lanes()
    rows = _rows(workday=500, greenhouse=10)
    open_plan = dp.make_plan(rows, lanes)
    wd = [p for p in open_plan["partitions"] if p["provider"] == "workday"]
    assert len(wd) == 2 and all(p["quota"] == 0 and not p["probe"] for p in wd)
    assert open_plan["mac_lanes"] == {} and open_plan["boards"] == len(rows)
    blocked = dp.make_plan(rows, lanes, refused={"workday": 0.998, "greenhouse": 0.0})
    wd = [p for p in blocked["partitions"] if p["provider"] == "workday"]
    assert sum(p["quota"] for p in wd) == 40 and all(p["probe"] for p in wd)
    assert blocked["boards"] == len(rows)                      # still planned and counted: deferred, never dropped
    assert all(m.get("probe") for m in dp.matrix(blocked) if m["provider"] == "workday")
    half = dp.make_plan(rows, lanes, refused={"workday": 0.5})
    assert all(p["quota"] == 0 for p in half["partitions"] if p["provider"] == "workday")


def test_the_refusal_share_comes_from_each_providers_latest_run(tmp_path):
    db = tmp_path / "jobs.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE scrape_log (id INTEGER PRIMARY KEY, run_id TEXT, source TEXT, company_slug TEXT, "
                "error_type TEXT)")
    rows = [("old", "workday", f"w{i}", None) for i in range(10)]
    rows += [("new", "workday", f"w{i}", "RateLimited" if i < 9 else None) for i in range(10)]
    rows += [("new", "lever", "l1", None)]
    con.executemany("INSERT INTO scrape_log (run_id, source, company_slug, error_type) VALUES (?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    assert dp.refused_shares(db) == {"workday": 0.9, "lever": 0.0}
    assert dp.refused_shares(tmp_path / "none.db") == {}


# -- the job-search sites: exported, merged, signed -------------------------------------------------------------

SCHEMA = ("CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT, company_slug TEXT, external_id TEXT, title TEXT, "
          "found_at TEXT, last_seen_at TEXT, closed_at TEXT, is_open INTEGER, "
          "UNIQUE (source, company_slug, external_id))")


def _db(path: Path, jobs, boards=("greenhouse",)):
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    con.execute("CREATE TABLE companies (source TEXT, slug TEXT, last_scraped_at TEXT)")
    con.executemany("INSERT INTO companies VALUES (?, 'x', '')", [(b,) for b in boards])
    con.executemany("INSERT INTO jobs (source, company_slug, external_id, title, found_at, last_seen_at, closed_at, "
                    "is_open) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", jobs)
    con.commit()
    con.close()


def test_only_the_sites_rows_this_job_touched_are_handed_on(tmp_path):
    db = tmp_path / "jobs.db"
    _db(db, [("adzuna", "acme", "1", "SRE", "2026-11-26 07:01:00", "2026-11-26 07:01:00", None, 1),        # new
             ("jooble", "beta", "2", "DevOps", "2026-11-01 00:00:00", "2026-11-26 07:02:00", None, 1),     # seen
             ("jooble", "gone", "3", "Old", "2026-10-01 00:00:00", "2026-11-04 00:00:00",
              "2026-11-26 07:03:00", 0),                                                                    # aged out
             ("jooble", "idle", "4", "Idle", "2026-11-20 00:00:00", "2026-11-20 00:00:00", None, 1),       # untouched
             ("greenhouse", "x", "5", "Board", "2026-11-26 07:01:00", "2026-11-26 07:01:00", None, 1)])    # a board's
    n, sources = dfeeds.export(db, tmp_path / "feeds.db", datetime(2026, 11, 26, 7, 0))
    assert (n, sources) == (3, ["adzuna", "jooble"])


def test_the_join_merges_the_sites_rows_and_never_a_boards(tmp_path):
    db = tmp_path / "jobs.db"
    _db(db, [("jooble", "beta", "2", "DevOps", "2026-11-01", "2026-11-20", None, 1),
             ("greenhouse", "x", "5", "Board", "2026-11-01", "2026-11-20", None, 1)])
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    _db(feeds / "feeds.db", [("jooble", "beta", "2", "DevOps", "2026-11-01", "2026-11-26", None, 1),
                             ("adzuna", "acme", "1", "SRE", "2026-11-26", "2026-11-26", None, 1),
                             ("greenhouse", "x", "5", "CHANGED", "2026-11-01", "2026-11-26", None, 0)])
    (feeds / "feeds-report.json").write_text(json.dumps({
        "read": ["adzuna", "jooble"], "row_sources": ["adzuna", "jooble"], "minutes": 4.2,
        "sources": {"adzuna": {"status": "read", "new": 1, "seen": 0, "secret": "x"},
                    "alerts": {"status": "skipped", "why": "no dedicated job-alert mailbox is configured"}}}))
    got = dm.merge_feeds(db, feeds)
    assert got["arrived"] and (got["added"], got["updated"]) == (1, 1)
    assert "secret" not in json.dumps(got) and got["sources"]["alerts"]["status"] == "skipped"
    con = sqlite3.connect(db)
    assert con.execute("SELECT last_seen_at FROM jobs WHERE external_id = '2'").fetchone()[0] == "2026-11-26"
    assert con.execute("SELECT title, is_open FROM jobs WHERE source = 'greenhouse'").fetchone() == ("Board", 1)
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    con.close()
    assert dm.merge_feeds(db, tmp_path / "nothing")["arrived"] is False
    assert dm.merge_feeds(db, None)["arrived"] is False


def test_the_result_names_the_sites_read_and_rejects_a_board_site_among_them(tmp_path):
    db = tmp_path / "jobs.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE companies (source TEXT, slug TEXT, last_scraped_at TEXT); "
                      "CREATE TABLE jobs (id INTEGER);"
                      "INSERT INTO companies VALUES ('greenhouse', 'a', '2026-11-26 07:30:00');")
    con.commit()
    con.close()
    manifest = {"plan_id": "", "partitions": [{"id": "greenhouse-1of1", "provider": "greenhouse", "run": True,
                                                "boards": [{"source": "greenhouse", "slug": "a"}]}]}
    manifest["plan_id"] = dr.plan_id_of([{"id": "greenhouse-1of1", "boards": [["greenhouse", "a"]]}])
    coverage = {"status": "COMPLETE", "partitions": [{"id": "greenhouse-1of1", "status": "COMPLETE", "reason": ""}]}
    outcomes = [["greenhouse", "a", "complete", "", "greenhouse-1of1"]]
    feeds = {"read": ["usajobs", "adzuna"], "row_sources": ["adzuna", "usajobs"]}
    result = dr.build(db, manifest, outcomes, coverage, "pw", started="2026-11-26T07:00:00Z", feeds=feeds)
    got = dr.verify(result, db, "pw")
    assert got["feeds_read"] == ["adzuna", "usajobs"] and got["feed_sources"] == {"adzuna", "usajobs"}
    bad = json.loads(json.dumps(result))
    bad["feeds"]["sources"].append("greenhouse")
    bad["signature"] = dr.sign(bad, "pw")
    with pytest.raises(dr.ResultError):
        dr.verify(bad, db, "pw")
    plain = dr.build(db, manifest, outcomes, coverage, "pw", started="2026-11-26T07:00:00Z")
    assert dr.verify(plain, db, "pw")["feed_sources"] == set()


# -- the workflow: started from the cloud, once per window, the sites' keys in one job only ----------------------

def test_the_workflow_runs_from_the_cloud_twice_a_day_with_the_keys_in_the_feeds_job_only():
    import yaml
    wf = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/daily-scrape.yml").read_text())
    on = wf.get("on") or wf.get(True)
    assert on["schedule"] == [{"cron": "23 6,7,16,17 * * *"}]
    assert on["workflow_dispatch"]["inputs"]["trigger"]["default"] == "manual"
    jobs = wf["jobs"]
    assert jobs["plan"]["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["scrape"]["if"] == jobs["feeds"]["if"] == "needs.plan.outputs.go == 'true'"
    assert "needs.plan.outputs.go == 'true'" in jobs["join"]["if"]
    for name, job in jobs.items():
        text = json.dumps(job)
        assert ("ADZUNA_APP_KEY" in text) == (name == "feeds"), name
        assert ("ALERTS_MAIL_APP_PASSWORD" in text) == (name == "feeds"), name


def test_the_feeds_job_reads_what_has_keys_reports_the_rest_and_hands_on_its_rows(tmp_path, monkeypatch):
    from data_engineering.db import session as session_mod
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session, init_engine
    from data_engineering.scraper import adzuna
    from data_engineering.scraper import feeds as feeds_pkg

    url = f"sqlite:///{tmp_path / 'jobs.db'}"
    init_engine(url)
    try:
        def fake_feeds(cfg):
            with get_session() as s:
                s.add(Job(source="jooble", company="Acme", company_slug="acme", external_id="j1", title="SRE",
                          application_url="https://example.com/j1", content_hash="h", status="Not Applied"))
            return [feeds_pkg.FeedSummary("jooble", "Jooble", calls=2, found=5, matched=1, new=1),
                    feeds_pkg.FeedSummary("usajobs", "USAJobs", skipped="needs USAJOBS_API_KEY in .env"),
                    feeds_pkg.FeedSummary("themuse", "The Muse", calls=1, error="FetchError: HTTP 404")]
        monkeypatch.setattr(adzuna, "credentials", lambda: None)
        monkeypatch.setattr(feeds_pkg, "run_feeds", fake_feeds)
        from data_engineering.scraper import workable_feed
        monkeypatch.setattr(workable_feed, "run", lambda cfg, **kw: workable_feed.Summary(why="not in this test"))
        monkeypatch.delenv("MAIL_ADDRESS", raising=False)
        monkeypatch.delenv("MAIL_APP_PASSWORD", raising=False)

        class Cfg:
            database_url = url

            def get(self, key, default=None):
                return default

        report = dfeeds.run(Cfg(), tmp_path / "out")
    finally:
        session_mod._engine = None
        session_mod._SessionFactory = None
    assert report["read"] == ["jooble"]
    assert report["sources"]["adzuna"]["status"] == "skipped" and report["sources"]["themuse"]["status"] == "failed"
    assert report["sources"]["usajobs"]["status"] == "skipped" and report["sources"]["alerts"]["status"] == "skipped"
    assert report["sources"]["workable-feed"]["status"] == "skipped"
    assert (report["rows"], report["row_sources"]) == (1, ["jooble"])
    saved = json.loads((tmp_path / "out" / "feeds-report.json").read_text())
    assert saved["read"] == ["jooble"] and (tmp_path / "out" / "feeds.db").is_file()


def test_board_names_and_searched_titles_never_reach_the_public_job_log(tmp_path, monkeypatch, capfd):
    """The scrapers' own log lines name boards and titles; on a public repository the job log is public."""
    import logging

    from devops import discovery_worker as dw

    def noisy(*a, **k):
        logging.getLogger("data_engineering.scraper.runner").warning("FAILED secretboard/slug — 'secret title'")
        raise RuntimeError("stop")
    monkeypatch.setattr(dw, "run", noisy)
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"partitions": [{"id": "p", "provider": "x", "boards": []}]}))
    dw.main(["--manifest", str(manifest), "--partition", "p", "--out", str(tmp_path / "out")])
    files = [h.baseFilename for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
    assert files == [str(tmp_path / "worker-private.log")]
    monkeypatch.setattr(dfeeds, "run", noisy)
    assert dfeeds.main(["--out", str(tmp_path / "out2")]) == 1
    seen = capfd.readouterr()
    assert "secret" not in seen.out + seen.err
    logging.basicConfig(force=True)                        # leave logging as other tests expect it
