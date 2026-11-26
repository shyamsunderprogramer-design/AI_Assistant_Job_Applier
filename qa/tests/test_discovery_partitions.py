"""Discovery partitions: plan, worker, merge and workflow -- offline, fixtures only (Nov 2026).

Every scenario here runs the real planner (devops/discovery_plan.py), the real board reader
(data_engineering/scraper/runner.scrape_companies through devops/discovery_worker.py, with the real
PoliteClient) and the real merge (devops/discovery_merge.py), on throwaway SQLite files. The only thing
faked is the network: requests.Session.request answers from a small table of boards, so a Workday 429
is a real 429 meeting the real retry, cool-off and provider rule. No test reads a real job site.

    1 every board exactly once          9 partial coverage closes nothing it did not read
    2 the same plan every time         10 a newer row is never overwritten by an older one
    3 one job site per partition       11 an interrupted merge leaves the database as it was
    4 Workday 429s, others succeed     12 the same inputs give the same database
    5 a partition out of time          13 the Mac still unlocks and merges the result
    6 missing or corrupt results       14 the workflow: syntax, needs, conditions, pins
    7 a result delivered twice         15 nothing here reaches applying or tailoring
    8 a partition run again
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests
import yaml

from data_engineering.db import session as session_mod
from data_engineering.db.models import Company, Job
from data_engineering.db.session import get_session, init_engine
from data_engineering.scraper import runner
from data_engineering.scraper.base import RawJob
from data_engineering.scraper.http_client import HttpSettings, PoliteClient
from devops import discovery_merge as dm
from devops import discovery_plan as dp
from devops import discovery_worker as dw

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "daily-scrape.yml"
OLD = (datetime.utcnow() - timedelta(days=3)).replace(microsecond=0)

LANES = {"time_budget_minutes": 5, "default_one_partition_budget": 2, "lanes": {
    "greenhouse": {"partitions": 2, "max_partitions": 3, "budget": 2, "one_host": True},
    "workday": {"partitions": 1, "max_partitions": 4, "budget": 1, "one_host": False}}}

# The boards' world: what each board answers. A list: its postings. 429: refused every time.
# ("slow", s, ids): answers after s seconds.
WORLD: dict[str, object] = {}
CALLS: list[str] = []


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


def fake_request(self, method, url, timeout=None, **kw):
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    CALLS.append(slug)
    said = WORLD.get(slug, [])
    if said == 429:
        return Resp(429)
    if said == 500:
        return Resp(500)
    if isinstance(said, tuple) and said[0] == "slow":
        time.sleep(said[1])
        said = said[2]
    return Resp(200, list(said))


class FakeBoards:
    source = ""

    def __init__(self, client):
        self.client = client

    def host(self, slug):
        return "boards-api.greenhouse.io" if self.source == "greenhouse" else f"{slug}.wd5.myworkdayjobs.com"

    def board_url(self, slug):
        return f"https://{self.host(slug)}/{slug}"

    def fetch_jobs(self, company):
        ids = self.client.get(self.board_url(company.slug)).json()
        return [RawJob(source=company.source, company=company.name, company_slug=company.slug, external_id=i,
                       title="Data Engineer", location="Remote", description="Build pipelines.",
                       application_url=f"https://example.com/{i}") for i in ids]


class GH(FakeBoards):
    source = "greenhouse"


class WD(FakeBoards):
    source = "workday"


class KeepAll:
    def apply(self, jobs):
        return list(jobs)

    def matches_stub(self, job):
        return True

    def reject_reason(self, job):
        return None


class Cfg:
    user_agent = "test-agent"

    def __init__(self, **over):
        self.d = {"http.respect_robots": False, "http.min_delay_seconds": 0.001, "http.jitter_seconds": 0.0,
                  "http.max_retries": 1, "http.backoff_base_seconds": 0.001, "http.backoff_jitter_fraction": 0.0,
                  "http.provider_trip_hosts": 3, "limits.scrape_workers": 2, **over}

    def get(self, key, default=None):
        return self.d.get(key, default)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    WORLD.clear()
    CALLS.clear()
    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(runner, "SCRAPER_TYPES", {"greenhouse": GH, "workday": WD})
    monkeypatch.setattr(runner, "resolve_filter", lambda cfg: KeepAll())
    yield
    session_mod._engine = None
    session_mod._SessionFactory = None


def _close_engine():
    if session_mod._engine is not None:
        session_mod._engine.dispose()
    session_mod._engine = None
    session_mod._SessionFactory = None


def seed(path: Path, boards: dict[tuple, list[str]]) -> Path:
    """A saved database: these boards, each with these open postings, last read three days ago."""
    init_engine(f"sqlite:///{path}")
    with get_session() as s:
        for (source, slug), ids in boards.items():
            s.add(Company(name=slug.title(), slug=slug, source=source, origin="seed", active=True,
                          last_scraped_at=OLD))
            for i in ids:
                s.add(Job(source=source, company=slug.title(), company_slug=slug, external_id=i,
                          title="Data Engineer", location="Remote", description="d", requirements="d",
                          application_url=f"https://example.com/{i}", content_hash=i, status="Not Applied",
                          is_open=True, last_seen_at=OLD))
    _close_engine()
    return path


BASE_BOARDS = {("greenhouse", "acme"): ["acme-old", "acme-1"], ("greenhouse", "beta"): ["beta-1"],
               ("greenhouse", "cyan"): ["cyan-1"], ("greenhouse", "dune"): [],
               ("workday", "w1"): ["w1-old"], ("workday", "w2"): [], ("workday", "w3"): [],
               ("workday", "w4"): ["w4-old"], ("workday", "w5"): []}


def healthy_world():
    WORLD.update({"acme": ["acme-1", "acme-2"], "beta": ["beta-1"], "cyan": ["cyan-1", "cyan-9"], "dune": ["dune-1"],
                  "w1": ["w1-old", "w1-new"], "w2": ["w2-1"], "w3": [], "w4": ["w4-old"], "w5": ["w5-1"]})


def plan_for(base: Path, lanes=LANES, **kw) -> dict:
    return dp.make_plan(dp.boards(base), lanes, **kw)


def run_partition(tmp: Path, base: Path, manifest: dict, pid: str, cfg=None, budget=5.0, attempt="1") -> Path:
    """One partition on 'its own machine': its own copy of the saved database, its own result."""
    home = tmp / f"machine-{pid}-{attempt}"
    home.mkdir(parents=True, exist_ok=True)
    db = home / "jobs.db"
    shutil.copy2(base, db)
    init_engine(f"sqlite:///{db}")
    try:
        result = dw.run(cfg or Cfg(), manifest, pid, budget, run_id=f"R-{attempt}-{pid}")
        part = dw.export(dw.database_file(), result, manifest, home / "out")
        (home / "out" / "report.json").write_text(json.dumps(dw.report(result)))
    finally:
        _close_engine()
    return part


def run_all(tmp: Path, base: Path, manifest: dict, **kw) -> dict[str, Path]:
    return {p["id"]: run_partition(tmp, base, manifest, p["id"], **kw) for p in manifest["partitions"]}


@contextmanager
def opened(path):
    """A connection that commits and CLOSES: a test's idle connection must not hold a database open."""
    con = sqlite3.connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


def dump(db: Path) -> str:
    with opened(db) as c:
        return "\n".join(c.iterdump())


def jobs_of(db: Path) -> list[tuple]:
    with opened(db) as c:
        return c.execute("SELECT source, company_slug, external_id, is_open FROM jobs ORDER BY 1, 2, 3").fetchall()


def open_ids(db: Path) -> set[str]:
    return {r[2] for r in jobs_of(db) if r[3]}


def joined(tmp: Path, base: Path, name="join.db") -> Path:
    target = tmp / name
    shutil.copy2(base, target)
    return target


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def synthetic_rows(n=500, seed_=7) -> list[dict]:
    rng = random.Random(seed_)
    sites = ["greenhouse", "workday", "bamboohr", "lever", "ashby", "breezy", "newsite"]
    return [{"source": rng.choice(sites), "slug": f"co{i:04d}", "name": f"Co {i}", "board_url": "", "origin": "x",
             "est_s": round(rng.uniform(0.2, 40), 3)} for i in range(n)]


# -- 1, 2, 3: the plan ---------------------------------------------------------------------------------

@pytest.mark.parametrize("target", [None, 8, 12, 16])
def test_1_every_board_is_in_exactly_one_partition(target):
    rows = synthetic_rows()
    lanes = dp.load_lanes()
    plan = dp.make_plan(rows, lanes, target)
    assigned = [(b["source"], b["slug"]) for p in plan["partitions"] for b in p["boards"]]
    cloud = [r for r in rows if not dp.mac_lane(lanes, r["source"])]
    assert len(assigned) == len(set(assigned)) == len(cloud) == plan["boards"]
    assert set(assigned) == {(r["source"], r["slug"]) for r in cloud}
    assert len({p["id"] for p in plan["partitions"]}) == len(plan["partitions"])
    # Every site is read in the cloud (25 Nov 2026): no Mac lane, every board in exactly one partition.
    assert not [r for r in rows if dp.mac_lane(lanes, r["source"])] and plan["mac_lanes"] == {}
    assert plan["boards"] == len(rows)


def test_2_the_same_boards_give_the_same_plan_whatever_their_order(tmp_path):
    rows = synthetic_rows()
    first = dp.make_plan(rows, dp.load_lanes(), 12)
    shuffled = rows[:]
    random.Random(1).shuffle(shuffled)
    again = dp.make_plan(shuffled, dp.load_lanes(), 12)
    assert first == again and first["plan_id"] == again["plan_id"]
    # Partition ids name the site and the split, never a board, and stay put for a given split.
    assert all(re.fullmatch(r"[a-z0-9_-]+-\d+of\d+", p["id"]) for p in first["partitions"])
    # From a database file too, read twice.
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    assert plan_for(base) == plan_for(base)
    # A different assignment is a different plan id.
    moved = dp.make_plan(rows[:-1], dp.load_lanes(), 12)
    assert moved["plan_id"] != first["plan_id"]


def test_3_a_partition_holds_one_job_site_only_and_budgets_never_grow():
    rows = synthetic_rows(900)
    lanes = dp.load_lanes()
    base = dp.make_plan(rows, lanes)
    for target in (None, 12, 16):
        plan = dp.make_plan(rows, lanes, target)
        for p in plan["partitions"]:
            assert {b["source"] for b in p["boards"]} == {p["provider"]}
        for site in {p["provider"] for p in plan["partitions"]}:
            lane = dp._lane(lanes, site)
            parts = [p for p in plan["partitions"] if p["provider"] == site]
            assert len(parts) <= dp.cap_of(lane)
            if lane["one_host"]:
                # One machine per allowed connection to the one host, one board at a time each.
                assert len(parts) <= lane["budget"] and all(p["workers"] == 1 for p in parts)
            else:
                # Split or not, the site's boards read at once stay its budget (at least one per partition).
                assert sum(p["workers"] for p in parts) == max(lane["budget"], len(parts))
        # Asking for more partitions never asks any site for more at once than the default plan did.
        for site in {p["provider"] for p in base["partitions"]}:
            lane = dp._lane(lanes, site)
            if lane["one_host"]:
                continue
            assert (sum(p["workers"] for p in plan["partitions"] if p["provider"] == site)
                    <= max(lane["budget"], sum(1 for p in plan["partitions"] if p["provider"] == site)))
    # A worker refuses a partition that names another site's board.
    mixed = dp.make_plan(rows, lanes)
    victim = mixed["partitions"][0]
    other = next(p for p in mixed["partitions"] if p["provider"] != victim["provider"])
    victim["boards"].append(other["boards"][0])
    with pytest.raises(ValueError):
        dw.find(mixed, victim["id"])


def test_the_public_matrix_names_no_board():
    plan = dp.make_plan(synthetic_rows(), dp.load_lanes(), 12)
    text = json.dumps(dp.matrix(plan))
    assert "co0" not in text and set(dp.matrix(plan)[0]) == {"id", "provider", "boards", "workers", "est_minutes"}


def test_only_some_partitions_run_is_never_taken_for_a_full_scan(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base, only={"greenhouse"})
    assert {p["provider"] for p in plan["partitions"] if p["run"]} == {"greenhouse"}
    parts = {p["id"]: run_partition(tmp_path, base, plan, p["id"]) for p in plan["partitions"] if p["run"]}
    cov = dm.merge(joined(tmp_path, base), plan, parts)
    assert cov["providers"]["workday"]["status"] == dm.NOT_ATTEMPTED
    assert cov["complete_refresh"] is False


# -- 4: Workday refuses, everything else carries on ---------------------------------------------------

def test_4_workday_429s_leave_workday_incomplete_and_greenhouse_complete(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    WORLD.update({w: 429 for w in ("w1", "w2", "w3", "w4", "w5")})
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan, budget=1.0)
    wd = next(p for p in plan["partitions"] if p["provider"] == "workday")
    # Three different tenants refused every retry: the whole provider was left alone -- the last two
    # boards were never asked at all, not asked faster through another tenant.
    assert [c for c in CALLS if c.startswith("w")] == ["w1", "w1", "w2", "w2", "w3", "w3"]
    report = json.loads((parts[wd["id"]].parent / "report.json").read_text())
    assert report["counts"]["rate_limited"] == 5 and report["provider_rate_limits"]["workday"] == 6
    target = joined(tmp_path, base)
    cov = dm.merge(target, plan, parts)
    assert cov["providers"]["greenhouse"]["status"] == dm.COMPLETE, [e["reason"] for e in cov["partitions"]]
    assert cov["providers"]["workday"]["status"] == dm.PARTIAL
    assert cov["providers"]["workday"]["counts"]["rate_limited"] == 5
    # Every result arrived -- and that is NOT a refresh: Workday's boards were not read.
    assert cov["all_arrived"] is True and cov["complete_refresh"] is False and cov["status"] == dm.PARTIAL
    still = open_ids(target)
    assert {"w1-old", "w4-old"} <= still                # nothing closed on a board that was not read
    assert "acme-old" not in still and {"acme-2", "dune-1"} <= still


def test_a_long_retry_after_is_honoured_as_a_cool_off_not_a_shorter_one(monkeypatch):
    s = HttpSettings(user_agent="t", respect_robots=False, min_delay_seconds=0.001, jitter_seconds=0,
                     max_retries=2, backoff_base_seconds=0.001, rate_limit_cooldown_seconds=10)
    client = PoliteClient(s)
    calls = []

    def answer(self, method, url, timeout=None, **kw):
        calls.append(url)
        return Resp(429, headers={"Retry-After": "3600"})

    monkeypatch.setattr(requests.Session, "request", answer)
    with pytest.raises(Exception):
        client.get("https://x.wd1.myworkdayjobs.com/a")
    assert len(calls) == 1                              # not slept through, not asked again
    assert client.cooling_left("x.wd1.myworkdayjobs.com") > 3500


def test_retry_after_as_an_http_date_and_jitter_stays_bounded():
    when = (datetime.utcnow() + timedelta(seconds=120)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 100 < PoliteClient.retry_after_seconds(when) <= 121
    assert PoliteClient.retry_after_seconds("7") == 7
    assert PoliteClient.retry_after_seconds("soon") is None
    assert HttpSettings(user_agent="t").backoff_jitter_fraction <= 1


# -- 5: a partition runs out of time ------------------------------------------------------------------

def test_5_a_partition_out_of_time_reports_its_unread_boards_and_closes_none_of_them(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    lanes = {**LANES, "lanes": {**LANES["lanes"], "greenhouse": {"partitions": 1, "max_partitions": 1,
                                                                  "budget": 1, "one_host": True}}}
    WORLD.update({"acme": ("slow", 0.4, ["acme-1"]), "beta": ("slow", 0.4, []), "cyan": ("slow", 0.4, []),
                  "dune": ("slow", 0.4, [])})
    plan = plan_for(base, lanes)
    gh = next(p for p in plan["partitions"] if p["provider"] == "greenhouse")
    wd = next(p for p in plan["partitions"] if p["provider"] == "workday")
    parts = {gh["id"]: run_partition(tmp_path, base, plan, gh["id"], budget=0.5 / 60),
             wd["id"]: run_partition(tmp_path, base, plan, wd["id"])}
    target = joined(tmp_path, base)
    cov = dm.merge(target, plan, parts)
    entry = next(e for e in cov["partitions"] if e["id"] == gh["id"])
    assert entry["status"] == dm.PARTIAL and entry["counts"]["not_attempted"] >= 2
    assert entry["counts"]["complete"] >= 1
    # beta and cyan were never reached: their postings stay open though the 'board' now lists none.
    assert {"beta-1", "cyan-1"} <= open_ids(target)
    assert next(e for e in cov["partitions"] if e["id"] == wd["id"])["status"] == dm.COMPLETE


def test_a_partition_that_crashes_is_failed_and_the_others_still_merge(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    wd = next(p["id"] for p in plan["partitions"] if p["provider"] == "workday")
    parts[wd] = None                                    # its machine died: nothing arrived
    reports = {wd: dw.failed_report(plan, wd, "TimeoutError")}
    target = joined(tmp_path, base)
    cov = dm.merge(target, plan, parts, reports)
    assert next(e for e in cov["partitions"] if e["id"] == wd)["status"] == dm.FAILED
    assert cov["providers"]["greenhouse"]["status"] == dm.COMPLETE
    assert cov["complete_refresh"] is False and cov["status"] == dm.PARTIAL
    assert "w1-new" not in open_ids(target) and "acme-2" in open_ids(target)
    assert reports[wd]["counts"]["not_attempted"] == 5


# -- 6: missing or corrupt results ---------------------------------------------------------------------

def test_6_missing_corrupt_foreign_or_overreaching_results_are_failed_alone(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    good = run_all(tmp_path, base, plan)
    gh1, gh2 = [p["id"] for p in plan["partitions"] if p["provider"] == "greenhouse"]
    wd = next(p["id"] for p in plan["partitions"] if p["provider"] == "workday")

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database at all" * 50)
    truncated = tmp_path / "truncated.db"
    truncated.write_bytes(good[gh1].read_bytes()[:3000])
    foreign = tmp_path / "foreign.db"
    shutil.copy2(good[gh1], foreign)
    with opened(foreign) as c:
        c.execute("UPDATE meta SET value = ? WHERE key = 'plan_id'", (json.dumps("someotherplan"),))
    overreach = tmp_path / "overreach.db"
    shutil.copy2(good[gh1], overreach)
    with opened(overreach) as c:           # rows of a board another partition owns
        cols = [r[1] for r in c.execute("PRAGMA table_info(jobs)") if r[1] != "id"]
        row = dict(zip(cols, c.execute(f"SELECT {', '.join(cols)} FROM jobs LIMIT 1").fetchone()))
        row.update(source="workday", company_slug="w2", external_id="sneak")
        c.execute(f"INSERT INTO jobs ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))
    unread = tmp_path / "unread.db"
    shutil.copy2(good[wd], unread)
    with opened(unread) as c:              # postings of a board it says it did not read
        c.execute("UPDATE boards SET status = 'rate_limited' WHERE slug = 'w2'")

    for bad, why in ((None, "did not arrive"), (garbage, "cannot"), (truncated, ""), (foreign, "another plan"),
                     (overreach, "not its own")):
        target = joined(tmp_path, base, f"j-{why or 'trunc'}.db")
        before = jobs_of(target)
        cov = dm.merge(target, plan, {**good, gh1: bad})
        e = next(x for x in cov["partitions"] if x["id"] == gh1)
        assert e["status"] == dm.FAILED and why in e["reason"], (why, e)
        assert next(x for x in cov["partitions"] if x["id"] == gh2)["status"] == dm.COMPLETE
        assert cov["complete_refresh"] is False
        # The bad result changed nothing of its own boards; the good ones were merged.
        mine = {b["slug"] for b in next(p for p in plan["partitions"] if p["id"] == gh1)["boards"]}
        assert [r for r in jobs_of(target) if r[1] in mine] == [r for r in before if r[1] in mine]
        assert "w1-new" in open_ids(target)
    cov = dm.merge(joined(tmp_path, base, "j-unread.db"), plan, {**good, wd: unread})
    e = next(x for x in cov["partitions"] if x["id"] == wd)
    assert e["status"] == dm.FAILED and "did not read" in e["reason"]


# -- 7, 8, 12: delivered twice, run again, run the same ---------------------------------------------------

def test_7_a_result_delivered_twice_adds_nothing_the_second_time(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    target = joined(tmp_path, base)
    dm.merge(target, plan, parts)
    with opened(target) as c:
        counts = [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("companies", "jobs", "scrape_log")]
    snapshot = jobs_of(target)
    cov = dm.merge(target, plan, parts)
    with opened(target) as c:
        again = [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("companies", "jobs", "scrape_log")]
    assert again == counts and jobs_of(target) == snapshot
    assert all(e["merged"]["added"] == 0 and e["merged"]["logged"] == 0 for e in cov["partitions"])


def test_8_a_failed_partition_run_again_completes_the_run_without_doubling_anything(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    wd = next(p["id"] for p in plan["partitions"] if p["provider"] == "workday")
    target = joined(tmp_path, base)
    first = dm.merge(target, plan, {**parts, wd: None})          # attempt 1: workday's machine failed
    assert first["complete_refresh"] is False
    # "Re-run failed jobs": workday again on a new machine from the saved database the join kept.
    rerun = run_partition(tmp_path, target, plan, wd, attempt="2")
    second = dm.merge(target, plan, {**parts, wd: rerun})
    assert second["complete_refresh"] is True and second["status"] == dm.COMPLETE
    fresh = joined(tmp_path, base, "fresh.db")
    dm.merge(fresh, plan, parts)
    assert jobs_of(target) == jobs_of(fresh)                     # the same postings as one clean run
    with opened(target) as c:
        assert c.execute("SELECT COUNT(*) FROM jobs GROUP BY source, company_slug, external_id "
                         "HAVING COUNT(*) > 1").fetchall() == []


def test_12_the_same_inputs_give_the_same_database_and_coverage(tmp_path):
    results = []
    for n in (1, 2):
        home = tmp_path / f"run{n}"
        home.mkdir()
        base = seed(home / "base.db", BASE_BOARDS)
        WORLD.clear()
        healthy_world()
        WORLD.update({"w2": 429, "w3": 429, "w5": 429})
        plan = plan_for(base)
        parts = run_all(home, base, plan, budget=1.0)
        target = joined(home, base)
        cov = dm.merge(target, plan, parts)
        results.append((plan["plan_id"], jobs_of(target), cov["status"],
                        {s: p["counts"] for s, p in cov["providers"].items()}))
    assert results[0] == results[1]


# -- 9, 10, 11: closing, conflicts, interruption ------------------------------------------------------

def test_9_partial_coverage_closes_only_what_a_complete_read_no_longer_lists(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    WORLD.update({"w1": 500, "w4": 429, "beta": 500})     # failed, rate-limited, failed
    WORLD["w1"] = 500
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan, budget=1.0)
    target = joined(tmp_path, base)
    cov = dm.merge(target, plan, parts)
    still = open_ids(target)
    assert {"w1-old", "w4-old", "beta-1"} <= still        # boards not read: nothing of theirs closed
    assert "acme-old" not in still                        # read in full, no longer listed: closed
    assert cov["boards"]["failed"] == 2 and cov["boards"]["rate_limited"] >= 1
    with opened(target) as c:
        assert c.execute("SELECT is_open, closed_at IS NOT NULL FROM jobs WHERE external_id = 'acme-old'"
                         ).fetchone() == (0, 1)


def test_10_a_newer_row_already_saved_is_never_overwritten_by_an_older_one(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    target = joined(tmp_path, base)
    future = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.000000")
    with opened(target) as c:                    # saved meanwhile by something newer
        c.execute("UPDATE jobs SET title = 'NEWER', last_seen_at = ? WHERE external_id = 'acme-1'", (future,))
        c.execute("UPDATE companies SET name = 'NEWER', last_scraped_at = ? WHERE slug = 'beta'", (future,))
    cov = dm.merge(target, plan, parts)
    with opened(target) as c:
        assert c.execute("SELECT title FROM jobs WHERE external_id = 'acme-1'").fetchone()[0] == "NEWER"
        assert c.execute("SELECT name FROM companies WHERE slug = 'beta'").fetchone()[0] == "NEWER"
        assert c.execute("SELECT is_open FROM jobs WHERE external_id = 'acme-2'").fetchone() == (1,), (
            [(e["id"], e["status"], e["reason"], e["merged"]) for e in cov["partitions"]],
            c.execute("SELECT company_slug, external_id FROM jobs").fetchall())
    assert sum(e["merged"].get("older_skipped", 0) for e in cov["partitions"]) == 1


def test_11_an_interrupted_merge_leaves_the_database_as_it_was_and_the_next_one_recovers(tmp_path, monkeypatch):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    target = joined(tmp_path, base)
    before = dump(target)

    real_replace, cut = os.replace, {"on": True}

    def power_cut(src, dst):
        if cut["on"]:
            raise OSError("interrupted")
        return real_replace(src, dst)

    monkeypatch.setattr(dm.os, "replace", power_cut)
    with pytest.raises(OSError):
        dm.merge(target, plan, parts)
    assert dump(target) == before                         # the one step never happened: contents as they were
    cut["on"] = False                                     # power back (never monkeypatch.undo(): conftest)
    (target.with_name(target.name + ".merging")).write_bytes(b"half-written")
    cov = dm.merge(target, plan, parts)                   # the left-over copy is discarded, not used
    assert cov["status"] == dm.COMPLETE and not target.with_name(target.name + ".merging").exists()
    fresh = joined(tmp_path, base, "fresh.db")
    dm.merge(fresh, plan, parts)
    assert jobs_of(target) == jobs_of(fresh)


def test_a_merge_failing_midway_through_one_partition_keeps_none_of_it(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    wd = next(p["id"] for p in plan["partitions"] if p["provider"] == "workday")
    target = joined(tmp_path, base)
    with opened(target) as c:                    # fails at the scrape log, after its postings went in
        c.execute(f"CREATE TRIGGER stop BEFORE INSERT ON scrape_log WHEN NEW.run_id = 'R-1-{wd}' "
                  "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    cov = dm.merge(target, plan, parts)
    e = next(x for x in cov["partitions"] if x["id"] == wd)
    assert e["status"] == dm.FAILED and "nothing of it was kept" in e["reason"]
    assert "w1-new" not in open_ids(target) and "w1-old" in open_ids(target)
    assert "acme-2" in open_ids(target)
    with opened(target) as c:
        assert c.execute("SELECT last_scraped_at FROM companies WHERE slug = 'w1'").fetchone()[0].startswith(
            OLD.strftime("%Y-%m-%d"))


def test_writes_still_in_the_wal_are_kept_and_a_database_open_elsewhere_is_refused(tmp_path):
    """The app keeps jobs.db in WAL mode: recent writes sit in jobs.db-wal. A merge that copied only jobs.db
    would lose them, and a -wal left beside the replaced file would be replayed onto it."""
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    live = joined(tmp_path, base, "live.db")
    holder = sqlite3.connect(live)                       # another program has it open, mid-day
    try:
        assert holder.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        holder.execute("PRAGMA wal_autocheckpoint = 0")
        holder.execute("UPDATE jobs SET title = 'SAVED IN WAL', last_seen_at = '2099-01-01 00:00:00' "
                       "WHERE external_id = 'beta-1'")             # newer than anything a partition read
        holder.commit()
        assert Path(str(live) + "-wal").stat().st_size > 0
        # Left behind by a crash: the file and its -wal, nobody holding them.
        crashed = tmp_path / "crashed.db"
        shutil.copy2(live, crashed)
        shutil.copy2(str(live) + "-wal", str(crashed) + "-wal")
        before = dump(live)
        with pytest.raises(dm.MergeError):               # open elsewhere: refused, nothing changed
            dm.merge(live, plan, parts)
        assert dump(live) == before
    finally:
        holder.close()
    cov = dm.merge(crashed, plan, parts)
    assert cov["status"] == dm.COMPLETE and not Path(str(crashed) + "-wal").exists()
    with opened(crashed) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT title FROM jobs WHERE external_id = 'beta-1'").fetchone()[0] == "SAVED IN WAL"
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE external_id = 'acme-2'").fetchone()[0] == 1


# -- completeness: a result arriving is not a refresh ------------------------------------------------------

def test_every_result_arriving_with_one_board_unread_is_not_a_complete_refresh(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    WORLD["beta"] = 500                                   # one board, of one partition, failed
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    cov = dm.merge(joined(tmp_path, base), plan, parts)
    assert cov["all_arrived"] is True and cov["complete_refresh"] is False
    assert [e["status"] for e in cov["partitions"]].count(dm.PARTIAL) == 1
    assert cov["providers"]["workday"]["status"] == dm.COMPLETE   # the others are still said to be complete
    # The command the workflow runs exits non-zero and says complete=false: the run fails, the Mac reads itself.
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps(plan))
    pdir = tmp_path / "parts"
    for pid, path in parts.items():
        (pdir / f"part-{pid}").mkdir(parents=True)
        shutil.copy2(path, pdir / f"part-{pid}" / "part.db")
    code = dm.main(["--db", str(joined(tmp_path, base, "cli.db")), "--manifest", str(manifest),
                    "--parts-dir", str(pdir), "--coverage", str(tmp_path / "c.json")])
    assert code == 3


def test_a_board_read_only_in_part_makes_its_partition_partial(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    wd = next(p["id"] for p in plan["partitions"] if p["provider"] == "workday")
    with opened(parts[wd]) as c:                           # e.g. more matches than its details budget
        c.execute("UPDATE boards SET status = 'partial' WHERE slug = 'w2'")
    cov = dm.merge(joined(tmp_path, base), plan, parts)
    assert next(e for e in cov["partitions"] if e["id"] == wd)["status"] == dm.PARTIAL
    assert cov["complete_refresh"] is False


def test_a_partition_whose_time_was_up_before_any_board_is_not_attempted_never_complete(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = {p["id"]: run_partition(tmp_path, base, plan, p["id"], budget=0.0) for p in plan["partitions"]}
    assert CALLS == []                                    # not one board was asked
    target = joined(tmp_path, base)
    cov = dm.merge(target, plan, parts)
    assert {e["status"] for e in cov["partitions"]} == {dm.NOT_ATTEMPTED}
    assert cov["status"] == dm.NOT_ATTEMPTED and cov["complete_refresh"] is False
    assert open_ids(target) == open_ids(base)             # nothing closed, nothing added
    assert cov["boards"]["not_attempted"] == sum(len(p["boards"]) for p in plan["partitions"])


def test_the_workday_pause_is_shared_by_every_worker_of_a_partition(tmp_path):
    """Scope, as built: one PoliteClient per partition (its machine), shared by all of that partition's
    worker threads -- so once three tenants refuse every retry, no worker of that partition asks Workday
    again. Two Workday partitions run on two machines and each keep their own count (not shared)."""
    boards = {("workday", f"t{i}"): [] for i in range(12)}
    base = seed(tmp_path / "base.db", boards)
    WORLD.update({f"t{i}": 429 for i in range(12)})
    lanes = {**LANES, "lanes": {"workday": {"partitions": 1, "max_partitions": 1, "budget": 4, "one_host": False}}}
    plan = plan_for(base, lanes)
    (part,) = plan["partitions"]
    assert part["workers"] == 4
    run_partition(tmp_path, base, plan, part["id"], budget=1.0)
    asked = {c for c in CALLS}
    # At most the three that tripped it plus the other workers already mid-request; never all twelve.
    assert 3 <= len(asked) <= 3 + (part["workers"] - 1)
    assert len(CALLS) <= len(asked) * 2                   # each asked at most once plus one retry


def _unpack_for_test(locked: Path, into: Path, passphrase: str) -> tuple[dict, Path]:
    """Unlock and unpack a verified result here, independent of the Mac's own code (its copy of this is
    devops/cloud_results._unpack, tested in test_discovery_import.py)."""
    import tarfile
    from devops import cloud_results
    raw = into / "result.tar"
    cloud_results._openssl(["-d", "-in", str(locked), "-out", str(raw)], passphrase)
    with tarfile.open(raw) as tar:
        assert sorted(m.name for m in tar.getmembers()) == ["jobs.db", "result.json"]
        for m in tar.getmembers():
            (into / m.name).write_bytes(tar.extractfile(m).read())
    return json.loads((into / "result.json").read_text()), into / "jobs.db"


def _step(job: str, name: str) -> str:
    flow = yaml.safe_load(WORKFLOW.read_text())
    script = next(s for s in flow["jobs"][job]["steps"] if s.get("name") == name)["run"]
    assert "${{" not in script                            # nothing spliced in: what runs here runs there
    return script


def _bash(script: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-e", "-c", script], cwd=cwd, env=env, capture_output=True, text=True)


@pytest.mark.parametrize("lose_one", [False, True])
def test_the_workflow_steps_themselves_plan_lock_merge_and_lock_for_the_mac(tmp_path, lose_one):
    """The step scripts from the workflow file, run as GitHub runs them (bash -e), on fixtures: plan,
    lock the plan, lock each partition's result, unlock and merge them, lock the database. Nothing is
    triggered and no job site is read: the partitions' results come from the offline worker."""
    from devops import cloud_results
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "python").symlink_to(sys.executable)
    temp, out, summ = tmp_path / "runner-temp", tmp_path / "gh-output", tmp_path / "gh-summary"
    temp.mkdir()
    env = {"PATH": f"{shim}:/usr/bin:/bin", "PYTHONPATH": str(ROOT), "PASSPHRASE": "pass-for-test",
           "RUNNER_TEMP": str(temp), "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summ),
           "PARTITIONS": "", "BUDGET": "", "ONLY": "", "PYTHONDONTWRITEBYTECODE": "1"}
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    # plan job
    planner = tmp_path / "plan-job"
    (planner / "data").mkdir(parents=True)
    shutil.copy2(base, planner / "data" / "jobs.db")
    done = _bash(_step("plan", "Give every board to exactly one partition"), planner, env)
    assert done.returncode == 0, done.stderr
    said = dict(line.split("=", 1) for line in out.read_text().splitlines())
    matrix = json.loads(said["matrix"])
    done = _bash(_step("plan", "Lock the plan"), planner, env)
    assert done.returncode == 0, done.stderr
    manifest = json.loads((temp / "manifest.json").read_text())
    assert [m["id"] for m in matrix] == [p["id"] for p in manifest["partitions"]]
    # scrape jobs: each partition's result locked by the workflow's own step
    joiner = tmp_path / "join-job"
    (joiner / "data").mkdir(parents=True)
    shutil.copy2(base, joiner / "data" / "jobs.db")
    (joiner / "plan").mkdir()
    shutil.copy2(planner / "manifest.json.enc", joiner / "plan" / "manifest.json.enc")
    runners = json.loads(said["runners"])
    assert sorted(pid for r in runners for pid in r["parts"].split()) == sorted(m["id"] for m in matrix)
    for r, runner in enumerate(runners):
        machine = tmp_path / f"runner-{runner['id']}"
        for i, pid in enumerate(runner["parts"].split()):
            part = run_partition(tmp_path, base, manifest, pid)          # the reader, offline
            if lose_one and r == 0 and i == 0:
                continue                                 # this reader died: no result
            (machine / "work" / pid / "out").mkdir(parents=True)
            shutil.copy2(part, machine / "work" / pid / "out" / "part.db")
        done = _bash(_step("scrape", "Lock these partitions' results"), machine, {**env, "PARTS": runner["parts"]})
        assert done.returncode == 0, done.stderr
        # download-artifact with merge-multiple: every runner's staged/ into parts/
        shutil.copytree(machine / "staged", joiner / "parts", dirs_exist_ok=True)
    # join job
    out.write_text("")
    done = _bash(_step("join", "Merge every partition whose result arrived"), joiner, env)
    assert done.returncode == 0, done.stderr
    said = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert said["merged"] == "true"
    assert said["complete"] == ("false" if lose_one else "true")
    coverage = json.loads((joiner / "coverage.json").read_text())
    assert (coverage["status"] == dm.COMPLETE) is (not lose_one)
    done = _bash(_step("join", "Lock the database for collection"), joiner, env)
    assert done.returncode == 0, done.stderr
    unlocked = tmp_path / "mac-unlocked.db"
    cloud_results._openssl(["-d", "-in", str(joiner / "jobs.db.enc"), "-out", str(unlocked)], "pass-for-test")
    assert jobs_of(unlocked) == jobs_of(joiner / "data" / "jobs.db")
    # The verified result, built and locked by the workflow's own step, unpacked and verified as the Mac does.
    env.update(STARTED="2000-01-01T00:00:00Z", RUN_ID="777", ATTEMPT="1",
               SCRAPE_JOBS="failure" if lose_one else "success")
    done = _bash(_step("join", "Build the verified result"), joiner, env)
    assert done.returncode == 0, done.stdout + done.stderr
    from devops import discovery_result
    unpack_to = tmp_path / "mac-result"
    unpack_to.mkdir()
    result, db = _unpack_for_test(joiner / discovery_result.LOCKED, unpack_to, "pass-for-test")
    checked = discovery_result.verify(result, db, "pass-for-test")
    assert checked["status"] == ("PARTIAL" if lose_one else "COMPLETE") or (lose_one and checked["status"] == "FAILED")
    assert result["run"]["id"] == "777" and result["run"]["execution"]["scrape_jobs"] == env["SCRAPE_JOBS"]
    assert result["complete_refresh"] is (not lose_one)
    assert "acme" not in summ.read_text() and "w1" not in summ.read_text()   # the public summary: counts only


# -- 13: the Mac's side, unchanged -----------------------------------------------------------------------

def test_13_the_merged_database_is_locked_and_unlocked_exactly_as_the_mac_expects(tmp_path, monkeypatch):
    from devops import cloud_results, merge_cloud
    flow = yaml.safe_load(WORKFLOW.read_text())
    join = flow["jobs"]["join"]["steps"]
    lock = next(s for s in join if s.get("name") == "Lock the database for collection")["run"]
    assert " ".join(cloud_results.CIPHER[1:]) in lock and "-in data/jobs.db -out jobs.db.enc" in lock
    upload = next(s for s in join if s.get("name") == "Upload it (a complete refresh)")
    assert upload["with"]["name"] == cloud_results.ARTIFACT and upload["with"]["path"] == "jobs.db.enc"
    assert upload["if"] == "steps.merge.outputs.complete == 'true'"
    from devops import discovery_result
    result = next(s for s in join if s.get("name") == "Upload the verified result")
    assert result["with"]["name"] == discovery_result.ARTIFACT != cloud_results.ARTIFACT
    assert result["if"] == "steps.merge.outputs.merged == 'true'"           # complete or not

    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    cloud = joined(tmp_path, base, "cloud.db")
    dm.merge(cloud, plan, parts)
    locked, unlocked = tmp_path / "jobs.db.enc", tmp_path / "unlocked.db"
    env = {**os.environ, "PASSPHRASE": "test-pass"}
    subprocess.run(["openssl", *cloud_results.CIPHER, "-salt", "-in", str(cloud), "-out", str(locked),
                    "-pass", "env:PASSPHRASE"], check=True, env=env, capture_output=True)
    cloud_results._openssl(["-d", "-in", str(locked), "-out", str(unlocked)], "test-pass")
    assert sha(unlocked) == sha(cloud)
    mac = seed(tmp_path / "mac.db", {})
    done = merge_cloud.merge(unlocked, mac, search=KeepAll())
    assert {"acme-2", "w1-new", "dune-1"} <= open_ids(mac), done


# -- 14: the workflow -------------------------------------------------------------------------------------

def test_14_the_workflow_parses_and_its_jobs_depend_on_each_other_safely():
    text = WORKFLOW.read_text()
    flow = yaml.safe_load(text)
    jobs = flow["jobs"]
    assert set(jobs) == {"plan", "scrape", "feeds", "join"}       # feeds: the job-search sites (25 Nov 2026)
    scrape, join = jobs["scrape"], jobs["join"]
    assert scrape["needs"] == "plan" and scrape["strategy"]["fail-fast"] is False
    # One runner per group of partitions (26 Nov 2026), each partition still its own reader.
    assert scrape["strategy"]["matrix"]["runner"] == "${{ fromJSON(needs.plan.outputs.runners) }}"
    assert scrape["name"].startswith("scrape ")              # devops/discovery_slot.py counts jobs named so
    assert scrape["strategy"]["max-parallel"] >= 16
    assert join["needs"] == ["plan", "scrape", "feeds"] and "!cancelled()" in join["if"] and "always()" not in join["if"]
    assert "needs.plan.result == 'success'" in join["if"]
    assert flow["concurrency"]["group"] == "daily-scrape" and flow["concurrency"]["cancel-in-progress"] is False
    # One pinned commit for every checkout, named once.
    assert re.fullmatch(r"[0-9a-f]{40}", flow["env"]["CODE_REF"])
    checkouts = [s for j in jobs.values() for s in j["steps"] if str(s.get("uses", "")).startswith("actions/checkout")]
    assert len(checkouts) == 4 and all(s["with"]["ref"] == "${{ env.CODE_REF }}" for s in checkouts)
    # Inputs reach a shell only through env, never spliced into a command.
    for j in jobs.values():
        for s in j["steps"]:
            assert "github.event.inputs" not in s.get("run", "")
    # The final artifact appears only when every partition delivered; a partial one has its own name.
    names = [s["with"]["name"] for s in join["steps"] if "upload-artifact" in str(s.get("uses", ""))]
    assert "jobs-db-encrypted" in names and "discovery-result-v1" in names
    # The last step names the outcome (devops/discovery_outcome.py): it always runs, and it fails the run only for a
    # genuine failure -- a partial result, verified and published, is a warning (26 Nov 2026, run #109).
    last = join["steps"][-1]
    assert last["if"] == "always()" and "devops.discovery_outcome" in last["run"]
    assert not any("complete != 'true'" in str(st.get("if", "")) and "exit 1" in st.get("run", "") for st in join["steps"])
    assert all(not st.get("continue-on-error") for st in join["steps"] if st.get("id") in ("lock", "build", "upload"))
    # Nothing unlocked travels: every uploaded file is .enc, or a counts-only report.
    for j in jobs.values():
        for s in j["steps"]:
            if "upload-artifact" in str(s.get("uses", "")):
                # staged/ is filled with locked results only (part-<partition>/part.db.enc); reports-staged/ with
                # the counts-only reports.
                assert s["with"]["path"].endswith((".enc", "report.json", "coverage.json")) or \
                    s["with"]["path"] in ("staged/", "reports-staged/"), s
    read = next(st for st in scrape["steps"] if st.get("name", "").startswith("Lock these partitions"))
    staged = [line for line in read["run"].splitlines() if "staged/part-" in line]
    assert staged and all("part.db.enc" in line or "mkdir" in line for line in staged)
    # The planner the plan job runs needs nothing installed.
    plan_src = (ROOT / "devops" / "discovery_plan.py").read_text()
    imports = set(re.findall(r"^(?:from|import) ([a-z_0-9]+)", plan_src, re.M))
    assert imports <= {"__future__", "argparse", "csv", "hashlib", "json", "re", "sqlite3", "statistics", "sys",
                       "pathlib", "heapq"}


CONTRACT = ("devops/discovery_plan.py", "devops/discovery_worker.py", "devops/discovery_merge.py",
            "devops/discovery_result.py", "devops/discovery_lanes.json", "devops/discovery_slot.py",
            "devops/discovery_feeds.py")


def test_the_pin_names_a_real_commit_holding_exactly_the_code_this_workflow_drives():
    """No placeholder, and no older commit: the pinned commit's plan, worker, merge and lanes are the ones in
    this tree. Move the pin when they change (README: one tested commit, changed on purpose)."""
    pin = yaml.safe_load(WORKFLOW.read_text())["env"]["CODE_REF"]
    assert re.fullmatch(r"[0-9a-f]{40}", pin) and set(pin) != {"0"}
    git = shutil.which("git")
    if git is None or subprocess.run([git, "rev-parse"], cwd=ROOT, capture_output=True).returncode != 0:
        pytest.skip("not a git checkout")
    assert subprocess.run([git, "cat-file", "-e", f"{pin}^{{commit}}"], cwd=ROOT).returncode == 0, "pin not found"
    for path in CONTRACT:
        pinned = subprocess.run([git, "show", f"{pin}:{path}"], cwd=ROOT, capture_output=True).stdout
        assert pinned == (ROOT / path).read_bytes(), f"{path} differs from the pinned commit {pin[:8]}"


@pytest.mark.parametrize("case", ["right", "wrong-commit", "no-merge", "old-merge"])
def test_the_pin_check_step_stops_a_wrong_pin(tmp_path, case):
    git = shutil.which("git")
    if git is None:
        pytest.skip("no git")
    repo = tmp_path / "checkout"
    (repo / "devops").mkdir(parents=True)
    for path in CONTRACT:
        shutil.copy2(ROOT / path, repo / path)
    if case == "no-merge":
        (repo / "devops" / "discovery_merge.py").unlink()
    if case == "old-merge":
        (repo / "devops" / "discovery_merge.py").write_text("print('delivered=true')\n")
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "HOME": str(tmp_path)}
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run([git, *args], cwd=repo, env=env, check=True, capture_output=True)
    head = subprocess.run([git, "rev-parse", "HEAD"], cwd=repo, env=env, capture_output=True, text=True).stdout.strip()
    env["CODE_REF"] = "1" * 40 if case == "wrong-commit" else head
    done = _bash(_step("plan", "Check the pinned code"), repo, env)
    assert (done.returncode == 0) is (case == "right"), done.stdout + done.stderr


def test_the_plan_command_prints_what_the_workflow_reads(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    out = tmp_path / "m.json"
    done = subprocess.run([sys.executable, "-m", "devops.discovery_plan", "plan", "--db", str(base),
                           "--target-total", "", "--budget-minutes", "", "--only", "", "--out", str(out)],
                          cwd=ROOT, capture_output=True, text=True, check=True)
    lines = dict(line.split("=", 1) for line in done.stdout.strip().splitlines())
    assert set(lines) == {"matrix", "runners", "plan_id", "budget"}
    assert json.loads(lines["matrix"]) == dp.matrix(json.loads(out.read_text()))
    assert "acme" not in done.stdout


# -- 15: no reach into applying, tailoring or the holds ----------------------------------------------------

def test_15_discovery_reaches_nothing_that_applies_tailors_or_lifts_a_hold():
    forbidden = ("backend.apply", "backend/apply", "ml.resume", "ml/resume", "auto_submit", "unattended_ai",
                 "submit", "cover_letter", "tailor", "HOLD_STARTUP_WATCHERS", "webapp")
    for name in ("discovery_plan.py", "discovery_worker.py", "discovery_merge.py"):
        src = (ROOT / "devops" / name).read_text()
        for word in forbidden:
            assert word not in src, (name, word)
    # What the workflow RUNS (not its comments, which say what it leaves out).
    flow = yaml.safe_load(WORKFLOW.read_text())
    commands = " ".join(str(s.get("run", "")) + str(s.get("uses", "")) + json.dumps(s.get("with", {}))
                        for j in flow["jobs"].values() for s in j["steps"]).lower()
    for word in ("tailor", "auto_submit", "backend.apply", "backend/apply", "cover_letter", "cover letter", "submit", "webapp", "ml.resume"):
        assert word not in commands, word
    # The merge never touches the person's own tables: only the three discovery tables.
    merge_src = (ROOT / "devops" / "discovery_merge.py").read_text()
    written = set(re.findall(r"INSERT INTO main\.([a-z_]+)", merge_src))
    assert written == {"companies", "jobs", "scrape_log"}
