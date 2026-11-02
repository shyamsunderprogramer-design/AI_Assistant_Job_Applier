"""The faster morning (docs/morning-run-speedup-plan.md, 2 Nov 2026): the GitHub fetch split four
ways by job site and joined back, every fresh run taken, the night's fetch added later, the 8 pm
start, and the board search waiting while the boards are read. Made-up runs and databases only:
no real GitHub call, no real database, no process touched."""

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine

from data_engineering.db.models import Base
from devops import cloud_results, fetch_parts, probe_runner

ROOT = Path(__file__).resolve().parents[2]


# -- made-up databases ----------------------------------------------------------------------------

def _row(db, table, **values):
    """A row with every required column filled by its type, then `values`."""
    required = {name: kind for _, name, kind, notnull, default, _ in db.execute(f"PRAGMA table_info({table})")
                if notnull and default is None and name != "id"}
    row = {name: ("2026-11-01 20:00:00" if "DATE" in kind.upper()
                  else 0 if kind.upper() in ("INTEGER", "BOOLEAN", "FLOAT") else "x")
           for name, kind in required.items()}
    row.update(values)
    db.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


def board(db, site, slug, **more):
    _row(db, "companies", **{"name": slug.title(), "slug": slug, "source": site, "origin": "csv", "active": 1, **more})


def posting(db, site, slug, ext, **more):
    _row(db, "jobs", source=site, company=slug.title(), company_slug=slug, external_id=ext, title="SRE",
         application_url="https://x", content_hash=ext, status="Not Applied", is_open=1, **more)


def logged(db, run, site, slug, seconds, at="2026-11-01 20:00:00"):
    _row(db, "scrape_log", run_id=run, source=site, company_slug=slug, ok=1, duration_seconds=seconds, created_at=at)


def new_db(path):
    Base.metadata.create_all(create_engine(f"sqlite:///{path}"))
    return path


# -- the split: by job site, balanced, every site in exactly one part -----------------------------

# Busy minutes per site from the Mac's fetch of 2 Nov 2026 (scrape_log, run 749aed235bd9).
BUSY = {"workday": 746, "bamboohr": 145, "greenhouse": 138, "lever": 69, "ashby": 48, "jazzhr": 42,
        "jobvite": 20, "workable": 18, "teamtailor": 16, "breezy": 16, "recruitee": 12}


@pytest.fixture
def measured(tmp_path):
    path = new_db(tmp_path / "cache.db")
    with sqlite3.connect(path) as db:
        for site, minutes in BUSY.items():
            board(db, site, f"{site}-co")
            logged(db, "older", site, f"{site}-co", minutes * 60 * 3, at="2026-10-30 20:00:00")
            logged(db, "latest", site, f"{site}-co", minutes * 60)
    return path


def test_the_sites_are_split_four_ways_each_in_exactly_one_part(measured):
    split = fetch_parts.plan(measured, 4)
    assert len(split) == 4 and split == fetch_parts.plan(measured, 4)          # the same every time
    named = [s for p in split for s in p["sites"].split(",") if s]
    rest = [p for p in split if not p["sites"]]
    assert len(named) == len(set(named)) and len(rest) == 1                     # none twice, one "the rest"
    assert sorted(rest[0]["skip"].split(",")) == sorted(named)                  # it leaves out exactly those
    assert {fetch_parts.owner_of(site, split) for site in BUSY} <= {p["name"] for p in split}
    # A site the plan never saw is still read, by the part that takes the rest.
    assert fetch_parts.owner_of("smartrecruiters", split) == rest[0]["name"]
    # Greenhouse keeps every board on one host: it sets the floor, about 2 h 18 min here.
    assert 130 <= fetch_parts.expected_minutes(measured, 4) <= 145
    gh_part = fetch_parts.owner_of("greenhouse", split)
    assert fetch_parts.owner_of("workday", split) != gh_part                    # the two longest apart


def test_the_split_reads_the_latest_run_and_counts_boards_without_one(tmp_path):
    path = new_db(tmp_path / "cache.db")
    with sqlite3.connect(path) as db:
        for i in range(30):
            board(db, "lever", f"l{i}")
        board(db, "ashby", "a0")
        board(db, "breezy", "gone", active=0)                                    # inactive: not read
    split = fetch_parts.plan(path, 4)
    named = {s for p in split for s in p["sites"].split(",") if s}
    assert "breezy" not in named and named <= {"lever", "ashby"}
    assert all(p["sites"] or p["skip"] or len(split) == 1 for p in split)       # no part names nothing


def test_with_nothing_to_split_one_part_reads_everything(tmp_path):
    assert fetch_parts.plan(new_db(tmp_path / "empty.db"), 4) == [{"name": "1", "sites": "", "skip": ""}]


def test_the_plan_command_writes_one_line_for_github(measured, capsys):
    assert fetch_parts.main(["plan", "--db", str(measured), "--parts", "4"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("parts=") and "\n" not in out
    assert len(json.loads(out[len("parts="):])) == 4


# -- the join: each site's rows from its own part, none missing, none twice ------------------------

@pytest.fixture
def parts(tmp_path):
    base = new_db(tmp_path / "base.db")
    with sqlite3.connect(base) as db:
        for site in ("greenhouse", "lever", "workday"):
            board(db, site, f"{site}-co")
            posting(db, site, f"{site}-co", "1")
            posting(db, site, f"{site}-co", "2")
        logged(db, "earlier", "greenhouse", "greenhouse-co", 2.0)
    split = [{"name": "1", "sites": "greenhouse", "skip": ""}, {"name": "2", "sites": "lever", "skip": ""},
             {"name": "3", "sites": "ashby", "skip": ""},
             {"name": "4", "sites": "", "skip": "ashby,greenhouse,lever"}]
    dbs = {}
    for p in split:
        dbs[p["name"]] = tmp_path / f"part-{p['name']}.db"
        shutil.copy(base, dbs[p["name"]])
    with sqlite3.connect(dbs["1"]) as db:                       # Greenhouse: one new, one edited, one closed
        posting(db, "greenhouse", "greenhouse-co", "3")
        db.execute("UPDATE jobs SET title = 'Staff SRE' WHERE source = 'greenhouse' AND external_id = '1'")
        db.execute("UPDATE jobs SET is_open = 0 WHERE source = 'greenhouse' AND external_id = '2'")
        db.execute("UPDATE companies SET last_scraped_at = '2026-11-02 21:00:00' WHERE source = 'greenhouse'")
        # Every part imports the whole board list; another site's row in this part is not its to give.
        db.execute("UPDATE jobs SET title = 'WRONG' WHERE source = 'lever'")
        logged(db, "run-1", "greenhouse", "greenhouse-co", 2.0)
    with sqlite3.connect(dbs["2"]) as db:                       # Lever: a new posting with the same new id
        posting(db, "lever", "lever-co", "3")
        board(db, "lever", "lever-new")
        logged(db, "run-2", "lever", "lever-co", 2.0)
    with sqlite3.connect(dbs["4"]) as db:                       # the rest: Workday, and a site new since
        posting(db, "workday", "workday-co", "3")
        board(db, "jazzhr", "jazz-co")
        posting(db, "jazzhr", "jazz-co", "9")
        logged(db, "run-4", "workday", "workday-co", 2.0)
    return base, split, dbs


def keys(path):
    with sqlite3.connect(path) as db:
        return sorted(db.execute("SELECT source, company_slug, external_id FROM jobs"))


def test_the_four_parts_join_with_nothing_missing_and_nothing_twice(parts):
    base, split, dbs = parts
    got = fetch_parts.join(base, split, dbs)
    assert got["added"] == 4 and got["parts"] == 4
    held = keys(base)
    assert len(held) == len(set(held)) == 10
    for name, path in dbs.items():                              # every posting a part read is here
        mine = [k for k in keys(path) if fetch_parts.owner_of(k[0], split) == name]
        assert set(mine) <= set(held)
    with sqlite3.connect(base) as db:
        rows = {(s, e): (t, o) for s, e, t, o in db.execute("SELECT source, external_id, title, is_open FROM jobs")}
        assert rows[("greenhouse", "1")] == ("Staff SRE", 1) and rows[("greenhouse", "2")][1] == 0
        assert rows[("lever", "1")][0] == "SRE"                  # part 1's word on Lever is not taken
        assert db.execute("SELECT last_scraped_at FROM companies WHERE source = 'greenhouse'").fetchone()[0] \
            == "2026-11-02 21:00:00"
        assert db.execute("SELECT COUNT(*) FROM companies WHERE slug IN ('lever-new', 'jazz-co')").fetchone()[0] == 2
        runs = sorted(r for (r,) in db.execute("SELECT run_id FROM scrape_log"))
        assert runs == ["earlier", "run-1", "run-2", "run-4"]   # each part's log once, the old one kept


def test_joining_the_same_parts_again_changes_nothing(parts):
    base, split, dbs = parts
    fetch_parts.join(base, split, dbs)
    once = keys(base)
    with sqlite3.connect(base) as db:
        logs = db.execute("SELECT COUNT(*) FROM scrape_log").fetchone()[0]
    again = fetch_parts.join(base, split, dbs)
    assert again["added"] == 0 and keys(base) == once
    with sqlite3.connect(base) as db:
        assert db.execute("SELECT COUNT(*) FROM scrape_log").fetchone()[0] == logs


def test_with_no_saved_copy_the_join_starts_from_a_part(parts, tmp_path):
    base, split, dbs = parts
    for name, path in dbs.items():
        (tmp_path / "parts" / f"part-{name}").mkdir(parents=True)
        shutil.copy(path, tmp_path / "parts" / f"part-{name}" / "part.db")
    (tmp_path / "plan.json").write_text(json.dumps(split))
    cold = tmp_path / "cold" / "jobs.db"
    assert fetch_parts.main(["join", "--db", str(cold), "--plan", str(tmp_path / "plan.json"),
                             "--parts-dir", str(tmp_path / "parts")]) == 0
    fetch_parts.join(base, split, dbs)
    assert keys(cold) == keys(base)


def test_a_missing_part_joins_nothing(parts):
    base, split, dbs = parts
    before = keys(base)
    dbs["3"].unlink()
    with pytest.raises(FileNotFoundError, match="part"):
        fetch_parts.join(base, split, dbs)
    assert keys(base) == before


def test_a_failure_halfway_leaves_the_base_as_it_was(parts, monkeypatch):
    base, split, dbs = parts
    before = keys(base)
    real = fetch_parts.owner_of
    calls = []

    def owner(site, plan):
        calls.append(site)
        if site == "workday":
            raise RuntimeError("boom")                          # the last part, after three joined
        return real(site, plan)
    monkeypatch.setattr(fetch_parts, "owner_of", owner)
    with pytest.raises(RuntimeError):
        fetch_parts.join(base, split, dbs)
    assert keys(base) == before


# -- one part reads only its own sites -------------------------------------------------------------

def test_a_part_reads_only_its_own_sites(monkeypatch):
    from data_engineering.db import session as session_mod
    from data_engineering.db.session import init_engine
    from data_engineering.scraper.discovery import upsert_company
    from data_engineering.scraper.runner import load_companies

    class Cfg:
        def get(self, path, default=None):
            return default
    init_engine("sqlite:///:memory:")
    try:
        for site in ("greenhouse", "lever", "workday"):
            upsert_company(site.title(), f"{site}-co", site)
        assert {c.source for c in load_companies(Cfg())} == {"greenhouse", "lever", "workday"}
        assert {c.source for c in load_companies(Cfg(), sites={"lever"})} == {"lever"}
        assert {c.source for c in load_companies(Cfg(), skip_sites={"lever", "greenhouse"})} == {"workday"}
    finally:
        session_mod._engine = None
        session_mod._SessionFactory = None


def test_the_scrape_command_takes_the_sites_and_passes_over_unknown_ones(monkeypatch, capsys):
    import main
    from data_engineering.scraper import runner

    seen = []

    class Summary:
        run_id, companies_attempted, companies_failed, jobs_seen, jobs_kept = "r", 0, 0, 0, 0
        jobs_new = jobs_updated = jobs_closed = jobs_reopened = 0
        failures = []
    monkeypatch.setattr(main, "init_engine", lambda url: None)
    monkeypatch.setattr(runner, "sync_seed_companies", lambda cfg: None)
    monkeypatch.setattr(runner, "run_scrape", lambda cfg, sites=None, skip=None: seen.append((sites, skip)) or Summary())

    class Cfg:
        database_url = "sqlite:///:memory:"
    args = main.build_parser().parse_args(["scrape", "--sites", "greenhouse, Lever", "--skip-sites", ""])
    assert main.cmd_scrape(Cfg(), args) == 0 and seen[-1] == ({"greenhouse", "lever"}, None)
    args = main.build_parser().parse_args(["scrape", "--skip-sites", "greenhouse,nosuch"])
    assert main.cmd_scrape(Cfg(), args) == 0 and seen[-1] == (None, {"greenhouse"})
    assert "nosuch" in capsys.readouterr().out
    args = main.build_parser().parse_args(["scrape"])
    assert main.cmd_scrape(Cfg(), args) == 0 and seen[-1] == (None, None)       # no option: every site


# -- taking the runs: every fresh one, once, oldest first -------------------------------------------

NOW = datetime.now(timezone.utc)


def stamp(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def done(run_id, finished_h, conclusion="success", took_h=2.3):
    return {"databaseId": run_id, "status": "completed", "conclusion": conclusion,
            "createdAt": stamp(finished_h + took_h), "updatedAt": stamp(finished_h)}


def going(run_id, started_h=0.5):
    return {"databaseId": run_id, "status": "in_progress", "conclusion": None,
            "createdAt": stamp(started_h), "updatedAt": stamp(0)}


@pytest.fixture
def gh(monkeypatch):
    """GitHub as a script: each look at the runs gives the next list (the last one repeats);
    starting a fetch is counted, never sent."""
    state = {"lists": [[]], "dispatched": 0, "taken": [], "fail": set()}

    def runs():
        lists = state["lists"]
        return lists.pop(0) if len(lists) > 1 else lists[0]

    def collect(run_id, started=""):
        if run_id in state["fail"]:
            raise cloud_results.CloudError("artifact expired")
        state["taken"].append(run_id)
        got = {"added": 10, "refreshed": 100, "closed": 1, "boards": 27000 + run_id, "run": run_id}
        cloud_results.COLLECTED.write_text(json.dumps({**got, "run": max(run_id, cloud_results._last_taken())}))
        return got

    def dispatch():
        state["dispatched"] += 1
    monkeypatch.setattr(cloud_results, "recent_runs", runs)
    monkeypatch.setattr(cloud_results, "collect", collect)
    monkeypatch.setattr(cloud_results, "dispatch", dispatch)
    monkeypatch.setattr(cloud_results, "usual_minutes", lambda: 140)
    monkeypatch.setattr(cloud_results, "_gh", lambda *a, **k: pytest.fail(f"real gh call: {a}"))
    return state


def taken_before(run_id):
    cloud_results.COLLECTED.write_text(json.dumps({"run": run_id, "added": 3, "refreshed": 0, "boards": 1}))


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_last_evenings_run_is_taken_at_once_and_the_night_is_asked_for(gh):
    taken_before(40)
    gh["lists"] = [[done(41, finished_h=8)]]
    said = []
    got = cloud_results.bring_in(say=said.append, sleep=lambda s: pytest.fail("waited"), trigger=True)
    assert gh["taken"] == [41] and got["added"] == 10
    assert gh["dispatched"] == 1 and json.loads(cloud_results.TOPUP.read_text())["after"] == 41
    assert any("night's postings are being fetched" in s for s in said)


def test_a_run_that_just_finished_needs_no_second_fetch(gh):
    gh["lists"] = [[done(41, finished_h=1)]]
    cloud_results.bring_in(say=[].append, trigger=True)
    assert gh["taken"] == [41] and gh["dispatched"] == 0 and not cloud_results.TOPUP.exists()


def test_githubs_own_run_under_way_is_the_nights_fetch(gh):
    gh["lists"] = [[going(42), done(41, finished_h=9)]]
    cloud_results.bring_in(say=[].append, trigger=True)
    assert gh["dispatched"] == 0 and json.loads(cloud_results.TOPUP.read_text())["after"] == 41


def test_several_runs_are_taken_once_each_oldest_first_and_counted_together(gh):
    taken_before(40)
    gh["lists"] = [[done(43, 2), done(42, 9), done(40, 12), done(39, 30)]]
    said = []
    got = cloud_results.bring_in(say=said.append, trigger=True)
    assert gh["taken"] == [42, 43]                              # 40 taken already, 39 a day old
    assert got["added"] == 20 and got["runs"] == [42, 43] and got["boards"] == 27043
    assert json.loads(cloud_results.COLLECTED.read_text())["run"] == 43
    assert sum("bringing in today's GitHub fetch" in s for s in said) == 2   # the Jobs page reads this line


def test_a_stale_or_failed_run_is_never_taken_and_the_mac_fetches(gh):
    gh["lists"] = [[done(41, finished_h=22), done(42, finished_h=2, conclusion="failure")]]
    said = []
    assert cloud_results.bring_in(say=said.append, trigger=False) is None
    assert gh["taken"] == [] and gh["dispatched"] == 0 and "fetching on this Mac" in said[-1]


def test_a_run_that_cannot_be_brought_in_does_not_stop_the_others(gh):
    gh["lists"] = [[done(42, 2), done(41, 3)]]
    gh["fail"] = {41}
    said = []
    got = cloud_results.bring_in(say=said.append, trigger=False)
    assert gh["taken"] == [42] and got["added"] == 10
    assert any("run 41 could not be brought in" in s for s in said)
    gh["fail"], gh["taken"] = {43}, []
    cloud_results.COLLECTED.unlink()
    gh["lists"] = [[done(43, 1)]]
    assert cloud_results.bring_in(say=said.append, trigger=False) is None     # none could: fetch here
    assert "fetching on this Mac" in said[-1]


def test_with_nothing_to_take_one_fetch_is_asked_for_and_waited_for(gh):
    clock = Clock()
    gh["lists"] = [[], [going(44, 0)], [going(44, 0.5)], [done(44, 0)]]
    said = []
    got = cloud_results.bring_in(say=said.append, sleep=clock.sleep, clock=clock, poll_s=60, trigger=True)
    assert gh["dispatched"] == 1 and gh["taken"] == [44] and got["run"] == 44
    assert not cloud_results.TOPUP.exists()                     # it is fresh: nothing more to ask
    assert sum("waiting for today's GitHub fetch" in s for s in said) == 1


def test_a_fetch_asked_for_that_never_shows_means_a_fetch_here(gh):
    clock = Clock()
    gh["lists"] = [[]]
    said = []
    assert cloud_results.bring_in(say=said.append, sleep=clock.sleep, clock=clock, poll_s=60, trigger=True) is None
    assert gh["dispatched"] == 1 and "fetching on this Mac" in said[-1]
    assert clock.t - 1_000_000 >= cloud_results.APPEAR_S


def test_switched_off_the_mac_never_starts_a_fetch(gh):
    gh["lists"] = [[done(41, finished_h=8)]]
    cloud_results.bring_in(say=[].append, trigger=False)
    gh["lists"] = [[]]
    assert cloud_results.bring_in(say=[].append, trigger=False) is None
    assert gh["dispatched"] == 0 and not cloud_results.TOPUP.exists()


def test_switched_off_only_a_run_started_today_counts_as_before(gh):
    yesterday = datetime.now().astimezone().replace(hour=0, minute=0, second=1) - timedelta(hours=2)
    old = {"databaseId": 41, "status": "completed", "conclusion": "success",
           "createdAt": yesterday.isoformat(), "updatedAt": yesterday.isoformat()}
    gh["lists"] = [[old]]
    assert cloud_results.bring_in(say=[].append, trigger=False) is None and gh["taken"] == []


def test_a_mac_asleep_past_the_deadline_still_takes_a_run_that_finished(gh):
    """The wait is on the wall clock: closed for five hours, the Mac wakes past its deadline, and
    the run that finished meanwhile is taken rather than the boards read all over again."""
    clock = Clock()
    gh["lists"] = [[going(45)], [done(45, 1)]]

    def asleep(seconds):
        clock.t += 5 * 3600
    got = cloud_results.bring_in(say=[].append, sleep=asleep, clock=clock, trigger=False)
    assert got and gh["taken"] == [45]


def test_a_run_already_in_is_not_fetched_again_the_same_morning(gh):
    taken_before(41)
    gh["lists"] = [[done(41, 1)]]
    said = []
    assert cloud_results.bring_in(say=said.append, trigger=True)["run"] == 41
    assert gh["taken"] == [] and gh["dispatched"] == 0 and "is in already" in said[-1]


# -- the night's postings, added when their fetch finishes ------------------------------------------

def ask(after, hours_ago=0.0, clock=None):
    at = (clock() if clock else datetime.now().timestamp()) - hours_ago * 3600
    cloud_results.TOPUP.write_text(json.dumps({"after": after, "at": at}))


def test_the_nights_fetch_is_waited_for_then_taken_in_a_separate_step(gh):
    """The wait brings nothing in and keeps the ask; the step run under the morning lock takes it."""
    clock = Clock()
    ask(41, clock=clock)
    taken_before(41)
    gh["lists"] = [[going(42), done(41, 8)], [going(42), done(41, 8)], [done(42, 0), done(41, 8)]]
    said = []
    assert cloud_results.wait_top_up(say=said.append, sleep=clock.sleep, clock=clock) is True
    assert gh["taken"] == [] and cloud_results.TOPUP.exists()
    assert sum("waiting for the night's GitHub fetch" in s for s in said) == 1
    got = cloud_results.top_up(say=said.append)
    assert gh["taken"] == [42] and got["run"] == 42 and not cloud_results.TOPUP.exists()


def test_the_step_under_the_lock_waits_for_nothing(gh):
    ask(41)
    taken_before(41)
    gh["lists"] = [[going(42), done(41, 8)]]
    said = []
    assert cloud_results.top_up(say=said.append) is None
    assert gh["taken"] == [] and len(gh["lists"]) == 1 and not cloud_results.TOPUP.exists()


def test_a_night_fetch_taken_meanwhile_is_not_added_twice(gh):
    """A morning run that ran during the wait took the night's fetch itself: nothing twice."""
    ask(41)
    taken_before(42)
    gh["lists"] = [[done(42, 0), done(41, 8)]]
    said = []
    assert cloud_results.top_up(say=said.append) is None
    assert gh["taken"] == [] and "in already" in said[-1] and not cloud_results.TOPUP.exists()


def test_a_night_fetch_that_cannot_be_brought_in_is_left_for_the_next_morning(gh):
    ask(41)
    taken_before(41)
    gh["fail"] = {42}
    gh["lists"] = [[done(42, 0), done(41, 8)]]
    said = []
    assert cloud_results.top_up(say=said.append) is None
    assert "could not be brought in" in said[-1] and not cloud_results.TOPUP.exists()
    assert cloud_results._last_taken() == 41                    # still not taken: the next run takes it


def test_no_ask_a_stale_ask_or_a_fetch_that_never_comes_adds_nothing(gh):
    clock = Clock()
    assert cloud_results.wait_top_up(say=[].append, clock=clock) is False      # nothing asked
    assert cloud_results.top_up(say=[].append) is None
    ask(41, hours_ago=7, clock=clock)
    said = []
    assert cloud_results.wait_top_up(say=said.append, clock=clock) is False
    assert "too long ago" in said[-1] and not cloud_results.TOPUP.exists()
    ask(41, clock=clock)
    taken_before(41)
    gh["lists"] = [[done(41, 8)]]
    assert cloud_results.wait_top_up(say=said.append, sleep=clock.sleep, clock=clock) is False
    assert "did not run or did not finish" in said[-1] and gh["taken"] == []
    assert not cloud_results.TOPUP.exists()
    ask(41, clock=clock)
    gh["lists"] = [[going(42), done(41, 8)]]
    assert cloud_results.wait_top_up(say=said.append, wait_s=600, sleep=clock.sleep, clock=clock) is False
    assert "taking too long" in said[-1] and not cloud_results.TOPUP.exists()
    cloud_results.TOPUP.write_text("{half")                                      # unreadable: dropped
    assert cloud_results.wait_top_up(say=said.append, clock=clock) is False
    assert not cloud_results.TOPUP.exists()


def test_github_out_of_reach_leaves_the_morning_list_as_it_is(gh, monkeypatch):
    def broken():
        raise cloud_results.CloudError("gh is not installed")
    monkeypatch.setattr(cloud_results, "recent_runs", broken)
    for step in (cloud_results.wait_top_up, cloud_results.top_up):
        ask(41)
        said = []
        assert not step(say=said.append)
        assert "list stays as it is" in said[-1] and not cloud_results.TOPUP.exists()


def test_the_wait_command_says_ready_by_its_exit(gh, monkeypatch):
    monkeypatch.setattr(cloud_results, "set_up", lambda: True)
    monkeypatch.setattr(cloud_results, "wait_top_up", lambda say=print: True)
    assert cloud_results.main(["wait-top-up"]) == 0
    monkeypatch.setattr(cloud_results, "wait_top_up", lambda say=print: False)
    assert cloud_results.main(["wait-top-up"]) == 1
    monkeypatch.setattr(cloud_results, "set_up", lambda: False)
    assert cloud_results.main(["wait-top-up"]) == 1


# -- 8 pm: the evening fetch ------------------------------------------------------------------------

def at(hour):
    return datetime.now().astimezone().replace(hour=hour, minute=5)


@pytest.fixture
def evening(gh, monkeypatch):
    monkeypatch.setattr(cloud_results, "set_up", lambda: True)
    monkeypatch.setattr(cloud_results, "trigger_on", lambda: True)
    return gh


def test_the_evening_fetch_starts_between_8_pm_and_midnight(evening):
    evening["lists"] = [[done(41, 30)]]
    assert cloud_results.evening(say=[].append, now=at(20)) == "asked" and evening["dispatched"] == 1
    assert cloud_results.evening(say=[].append, now=at(23)) == "asked"
    for hour in (0, 3, 19):                                     # woken after midnight, or too early
        assert cloud_results.evening(say=[].append, now=at(hour)) == "outside"
    assert evening["dispatched"] == 2


def test_the_evening_fetch_is_not_started_twice(evening):
    now = at(21)

    def before(hours):                                          # stamps as of 9:05 pm, not of now
        return (now - timedelta(hours=hours)).isoformat()
    evening["lists"] = [[{"databaseId": 42, "status": "in_progress", "conclusion": None,
                          "createdAt": before(0.2), "updatedAt": before(0)}]]
    assert cloud_results.evening(say=[].append, now=now) == "running"
    evening["lists"] = [[{"databaseId": 42, "status": "completed", "conclusion": "success",
                          "createdAt": before(2.8), "updatedAt": before(0.5)}]]
    assert cloud_results.evening(say=[].append, now=now) == "done"
    assert evening["dispatched"] == 0


def test_switched_off_the_evening_does_nothing(gh, monkeypatch):
    monkeypatch.setattr(cloud_results, "set_up", lambda: True)
    monkeypatch.setattr(cloud_results, "trigger_on", lambda: False)
    assert cloud_results.evening(say=[].append, now=at(20)) == "off" and gh["dispatched"] == 0
    monkeypatch.setattr(cloud_results, "set_up", lambda: False)
    assert cloud_results.evening(say=[].append, now=at(20)) == "not set up"


def test_the_switch_is_off_in_the_settings_as_shipped():
    settings = yaml.safe_load((ROOT / "backend" / "config" / "config.yaml").read_text())
    assert settings["cloud"]["trigger_runs"] is False
    assert cloud_results.trigger_on() is False


# -- the board search waits, and carries on from the same name --------------------------------------

def test_a_pause_is_asked_for_while_it_lasts_and_only_by_a_live_process():
    with probe_runner.paused("the job boards are read on this Mac"):
        with probe_runner.paused("nested"):
            assert probe_runner.pause_asked()["reason"] == "the job boards are read on this Mac"
        assert probe_runner.pause_asked()                       # the inner one ending ends nothing
    assert probe_runner.pause_asked() is None and not probe_runner.PAUSEFILE.exists()
    probe_runner.PAUSEFILE.write_text(json.dumps({"pid": 999_999, "reason": "killed", "at": 0}))
    assert probe_runner.pause_asked() is None                   # its process is gone
    import os
    import time
    probe_runner.PAUSEFILE.write_text(json.dumps({"pid": os.getpid(), "reason": "old",
                                                  "at": time.time() - probe_runner.PAUSE_STALE_S - 1}))
    assert probe_runner.pause_asked() is None                   # older than any fetch


def test_the_search_waits_between_batches_until_the_fetch_ends(monkeypatch, tmp_path):
    monkeypatch.setattr(probe_runner, "STATE", tmp_path / "probe_state.json")
    monkeypatch.setattr(probe_runner, "LOG", tmp_path / "probe_runner.log")
    slept = []
    with probe_runner.paused("the GitHub fetch's postings are brought in"):
        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                probe_runner.PAUSEFILE.unlink()                 # the merge finished
        assert probe_runner.wait_while_paused(lambda: False, sleep=sleep) is True
    assert slept == [probe_runner.PAUSE_POLL_S] * 3
    assert json.loads((tmp_path / "probe_state.json").read_text())["status"] == "running"
    assert "Pausing while the GitHub fetch's postings are brought in" in (tmp_path / "probe_runner.log").read_text()
    assert probe_runner.wait_while_paused(lambda: False, sleep=lambda s: pytest.fail("waited")) is False
    with probe_runner.paused("x"):                              # asked to stop: stops waiting
        assert probe_runner.wait_while_paused(lambda: True, sleep=lambda s: pytest.fail("waited")) is False


# -- the morning run: the boards read here only under a pause; the night's postings after -------------

class Cfg:
    def __init__(self, tmp_path):
        self.resume = tmp_path / "base_resume.txt"
        self.resume.write_text("JANE DOE\nSKILLS\nPython, AWS\nEXPERIENCE\n- Built AWS platforms\n")
        self.sheet = tmp_path / "t.xlsx"

    def get(self, path, default=None):
        return {"resume.base_path": str(self.resume), "limits.stale_company_warn_days": 0,
                "excel.path": str(self.sheet)}.get(path, default)


@pytest.fixture
def morning(tmp_path, monkeypatch):
    from data_engineering.db import session as session_mod
    from data_engineering.db.session import init_engine
    init_engine("sqlite:///:memory:")
    monkeypatch.setattr("data_engineering.scraper.feeds.run_feeds", lambda cfg, **kw: [])
    monkeypatch.setattr("data_engineering.scraper.adzuna.credentials", lambda: None)
    monkeypatch.setattr("data_engineering.scraper.runner.sync_seed_companies", lambda cfg: None)
    monkeypatch.delenv("MAIL_ADDRESS", raising=False)
    yield Cfg(tmp_path)
    session_mod._engine = None
    session_mod._SessionFactory = None


def test_the_boards_are_read_here_only_while_the_board_search_waits(morning, monkeypatch):
    from backend.core.daily import run_daily
    asked = []
    monkeypatch.setattr("data_engineering.scraper.runner.run_scrape",
                        lambda cfg: asked.append(probe_runner.pause_asked()))
    monkeypatch.setattr(cloud_results, "set_up", lambda: True)
    monkeypatch.setattr(cloud_results, "bring_in", lambda say=print: None)
    run_daily(morning, cloud=True)
    assert asked[0]["reason"] == "the job boards are read on this Mac"
    assert probe_runner.pause_asked() is None                   # and carries on after


def test_the_nights_postings_are_ranked_and_put_in_the_digest(morning, monkeypatch):
    from backend.core.daily import render_digest, run_top_up
    monkeypatch.setattr(cloud_results, "set_up", lambda: True)
    assert run_top_up(morning) is None                          # nothing asked this morning
    cloud_results.TOPUP.write_text("{}")
    monkeypatch.setattr(cloud_results, "top_up", lambda say=print: None)
    report = run_top_up(morning)
    assert [s.name for s in report.stages] == ["top-up"] and report.fetched_by == ""
    monkeypatch.setattr(cloud_results, "top_up", lambda say=print: {
        "added": 7, "refreshed": 900, "closed": 2, "reopened": 0, "boards": 27000, "run": 44})
    report = run_top_up(morning)
    assert [s.name for s in report.stages] == ["top-up", "score", "export", "digest"]
    assert report.jobs_new == 7 and "27000 boards scraped by the GitHub run" in render_digest(report, morning)


def test_the_top_up_command_says_when_there_is_nothing_to_add(monkeypatch, capsys):
    import main
    from backend.core import daily
    monkeypatch.setattr(main, "init_engine", lambda url: None)
    monkeypatch.setattr(daily, "run_top_up", lambda cfg, since_hours=24: None)

    class Cfg:
        database_url = "sqlite:///:memory:"
    args = main.build_parser().parse_args(["daily", "--top-up"])
    assert main.cmd_daily(Cfg(), args) == 0 and "nothing to add" in capsys.readouterr().out


# -- the workflow: four parts, each site once, locked between jobs ------------------------------------

def test_the_workflow_plans_four_parts_and_joins_them():
    flow = yaml.safe_load((ROOT / ".github" / "workflows" / "daily-scrape.yml").read_text())
    jobs = flow["jobs"]
    assert list(jobs) == ["plan", "scrape", "join"]
    assert "fetch_parts plan" in " ".join(s.get("run", "") for s in jobs["plan"]["steps"])
    assert jobs["scrape"]["needs"] == "plan"
    assert jobs["scrape"]["strategy"]["matrix"]["part"] == "${{ fromJSON(needs.plan.outputs.parts) }}"
    steps = {s.get("name"): s for s in jobs["scrape"]["steps"]}
    read = steps["Scrape this part's job sites"]
    assert '--sites "$SITES" --skip-sites "$SKIP"' in read["run"]       # names through env, never inline
    assert "-salt" in steps["Lock this part for the join"]["run"]
    assert steps["Hand it to the join"]["with"]["retention-days"] == 1
    # Only the join saves the database; every part starts from the same saved copy.
    uses = [s.get("uses", "") for job in jobs.values() for s in job["steps"]]
    assert uses.count("actions/cache/save@v4") == 1 and "actions/cache@v4" not in uses
    assert jobs["join"]["needs"] == ["plan", "scrape"]
    join = " ".join(s.get("run", "") for s in jobs["join"]["steps"])
    assert "fetch_parts join" in join and "jobs.db.enc" in join
    # The schedule stays as the backup, and the button the Mac presses is there.
    on = flow.get("on", flow.get(True))
    assert on["schedule"] == [{"cron": "17 6 * * *"}] and "workflow_dispatch" in on


def test_the_summary_counts_every_part_of_the_last_fetch(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("actions_summary", ROOT / "devops" / "actions_summary.py")
    summary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(summary)
    path = new_db(tmp_path / "jobs.db")
    with sqlite3.connect(path) as db:
        for i, run in enumerate(("p1", "p2", "p3", "p4")):
            _row(db, "scrape_log", run_id=run, source="greenhouse", company_slug=f"c{i}", ok=1, jobs_seen=10,
                 created_at=f"2026-11-02 2{i}:00:00")
        _row(db, "scrape_log", run_id="yesterday", source="greenhouse", company_slug="old", ok=1, jobs_seen=99,
             created_at="2026-11-01 08:00:00")
    assert "| Postings seen | 40 |" in summary.summary(path)
