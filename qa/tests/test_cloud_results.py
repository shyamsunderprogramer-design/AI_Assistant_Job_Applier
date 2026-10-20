"""The GitHub fetch's postings taken on a morning it read the boards, and the board list it reads."""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine

from data_engineering.db.models import Base
from devops import cloud_results, merge_cloud


# -- the merge: what the cloud saw taken down comes down here too ------------------------------

def make_db(path, rows):
    Base.metadata.create_all(create_engine(f"sqlite:///{path}"))
    with sqlite3.connect(path) as db:
        # Every other column a row must have, filled by its type.
        required = {name: kind for _, name, kind, notnull, default, _ in db.execute("PRAGMA table_info(jobs)")
                    if notnull and default is None and name != "id"}
        for ext, status, is_open, seen, closed in rows:
            row = {name: (seen if "DATE" in kind.upper() else 0 if kind.upper() in ("INTEGER", "BOOLEAN", "FLOAT")
                          else "x") for name, kind in required.items()}
            row.update(source="greenhouse", company="Acme", company_slug="acme", external_id=ext, title="SRE",
                       application_url="https://x", content_hash=ext, status=status, is_open=is_open,
                       last_seen_at=seen, closed_at=closed, found_at=seen)
            db.execute(f"INSERT INTO jobs ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                       list(row.values()))
    return path


def test_postings_the_cloud_saw_taken_down_come_down_here_and_statuses_stay(tmp_path):
    local = make_db(tmp_path / "local.db", [
        ("up", "Not Applied", 1, "2026-10-17 10:00:00", None),
        ("applied", "Applied", 1, "2026-10-17 10:00:00", None),
        ("stale", "Not Applied", 1, "2026-10-18 09:00:00", None),       # seen here after the cloud's "closed"
        ("back", "Closed", 0, "2026-10-15 10:00:00", "2026-10-16 10:00:00"),
    ])
    cloud = make_db(tmp_path / "cloud.db", [
        ("up", "Not Applied", 0, "2026-10-17 10:00:00", "2026-10-18 06:40:00"),
        ("applied", "Not Applied", 0, "2026-10-17 10:00:00", "2026-10-18 06:40:00"),
        ("stale", "Not Applied", 0, "2026-10-17 10:00:00", "2026-10-18 06:40:00"),
        ("back", "Not Applied", 1, "2026-10-18 06:40:00", None),
        ("new", "Not Applied", 1, "2026-10-18 06:40:00", None),
    ])
    got = merge_cloud.merge(cloud, local)
    assert (got["added"], got["closed"], got["reopened"]) == (1, 2, 1)
    with sqlite3.connect(local) as db:
        rows = {r[0]: r[1:] for r in db.execute("SELECT external_id, status, is_open, closed_at FROM jobs")}
    assert rows["up"] == ("Closed", 0, "2026-10-18 06:40:00")
    assert rows["applied"] == ("Applied", 0, "2026-10-18 06:40:00")          # "Applied" outlives the posting
    assert rows["stale"][1] == 1                                             # the Mac saw it up later
    assert rows["back"] == ("Not Applied", 1, None)
    assert "new" in rows


def test_what_the_board_says_now_reaches_a_posting_held_here(tmp_path):
    """On a morning the cloud read the boards, an employer's edit and the board's word for the kind
    of job reached only the cloud's copy: the merge kept the first words ever read here."""
    local = make_db(tmp_path / "local.db", [
        ("edited", "Applied", 1, "2026-10-19 07:00:00", None),
        ("typed", "Not Applied", 1, "2026-10-19 07:00:00", None),
        ("mine", "Not Applied", 1, "2026-10-19 07:00:00", None),
        ("later", "Not Applied", 1, "2026-10-20 10:00:00", None),       # read here after the cloud did
    ])
    cloud = make_db(tmp_path / "cloud.db", [
        (ext, "Not Applied", 1, "2026-10-20 06:40:00", None) for ext in ("edited", "typed", "mine", "later")])
    with sqlite3.connect(local) as db:
        db.execute("UPDATE jobs SET description = 'Build our CI/CD platform.', ats_match_score = 0.71")
        db.execute("UPDATE jobs SET job_type = 'part-time', job_type_manual = 1 WHERE external_id = 'mine'")
    with sqlite3.connect(cloud) as db:
        db.execute("UPDATE jobs SET description = 'Build our CI/CD platform.'")
        db.execute("UPDATE jobs SET title = 'SRE (Contract)', content_hash = external_id || '-2', "
                   "description = 'A 6-month contract. Build our CI/CD platform.' "
                   "WHERE external_id IN ('edited', 'later')")
        db.execute("UPDATE jobs SET stated_job_type = 'Contract' WHERE external_id IN ('typed', 'mine')")

    got = merge_cloud.merge(cloud, local)
    assert (got["edited"], got["typed"], got["added"]) == (1, 2, 0)
    with sqlite3.connect(local) as db:
        rows = {r[0]: r[1:] for r in db.execute(
            "SELECT external_id, title, description, job_type, stated_job_type, status, ats_match_score, "
            "exported_to_excel, last_seen_at FROM jobs")}
    assert rows["edited"][:4] == ("SRE (Contract)", "A 6-month contract. Build our CI/CD platform.", "contract", None)
    assert rows["edited"][4:7] == ("Applied", 0.71, 0)                   # the history here stays
    assert rows["typed"][2:4] == ("contract", "Contract")
    assert rows["mine"][2:4] == ("part-time", "Contract")                 # the person's own word stays
    assert rows["later"][0] == "SRE" and rows["later"][7] == "2026-10-20 10:00:00"   # newer here: kept

    again = merge_cloud.merge(cloud, local)                               # a run merged twice
    assert (again["edited"], again["typed"]) == (0, 0)


def test_two_companies_numbering_their_postings_alike_stay_apart(tmp_path):
    """BambooHR numbers each company's postings from 1: one company's posting 12 is not another's.
    Matched on board and id alone, a new posting 12 was never added, and an edit to one would have
    overwritten the other."""
    local = make_db(tmp_path / "local.db", [(e, "Not Applied", 1, "2026-10-19 07:00:00", None) for e in "ab"])
    cloud = make_db(tmp_path / "cloud.db", [(e, "Not Applied", 1, "2026-10-20 06:40:00", None) for e in "ac"])
    for path in (local, cloud):
        with sqlite3.connect(path) as db:
            db.execute("UPDATE jobs SET source = 'bamboohr', company_slug = external_id, external_id = '12'")
    with sqlite3.connect(cloud) as db:
        db.execute("UPDATE jobs SET title = 'Platform Engineer', content_hash = 'new' WHERE company_slug = 'a'")

    got = merge_cloud.merge(cloud, local)
    assert (got["added"], got["edited"]) == (1, 1)
    with sqlite3.connect(local) as db:
        assert dict(db.execute("SELECT company_slug, title FROM jobs")) == {
            "a": "Platform Engineer", "b": "SRE", "c": "SRE"}


# -- taking today's run: ready, under way, or none ---------------------------------------------

RUN = {"databaseId": 42, "status": "completed", "conclusion": "success",
       "createdAt": (datetime.now().astimezone() - timedelta(hours=1)).isoformat()}


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_results, "COLLECTED", tmp_path / "collected.json")
    monkeypatch.setattr(cloud_results, "BOARDS_SENT", tmp_path / "sent.txt")
    monkeypatch.setattr(cloud_results, "usual_minutes", lambda: 150)
    said = []
    return said


def run_with(monkeypatch, *states):
    runs = list(states)
    monkeypatch.setattr(cloud_results, "today_run", lambda: runs.pop(0) if len(runs) > 1 else runs[0])
    got = {"added": 12, "refreshed": 900, "closed": 3, "boards": 21000, "run": 42}

    def collect(run_id, started=""):
        cloud_results.COLLECTED.write_text(json.dumps({**got, "run": run_id}))
        return got
    monkeypatch.setattr(cloud_results, "collect", collect)
    return got


def test_a_finished_run_is_taken(cloud, monkeypatch):
    got = run_with(monkeypatch, RUN)
    assert cloud_results.bring_in(say=cloud.append, sleep=lambda s: None) == got
    assert any("bringing in" in s for s in cloud) and any(" seen again, " in s for s in cloud)
    # Once in, a second morning run takes nothing twice.
    assert cloud_results.state(RUN) == "collected"


def test_a_run_under_way_is_waited_for_not_fetched_again(cloud, monkeypatch):
    running = {**RUN, "status": "in_progress", "conclusion": None}
    got = run_with(monkeypatch, running, running, RUN)
    slept = []
    assert cloud_results.bring_in(say=cloud.append, sleep=slept.append, poll_s=60) == got
    waits = [s for s in cloud if "waiting for today's GitHub fetch" in s]
    assert len(waits) == 1 and "usually about 150 min" in waits[0] and slept == [60, 60]


def test_no_run_today_a_failed_one_or_too_long_a_wait_means_a_fetch_here(cloud, monkeypatch):
    run_with(monkeypatch, None)
    assert cloud_results.bring_in(say=cloud.append) is None and "fetching on this Mac" in cloud[-1]
    run_with(monkeypatch, {**RUN, "conclusion": "failure"})
    assert cloud_results.bring_in(say=cloud.append) is None
    run_with(monkeypatch, {**RUN, "status": "in_progress", "conclusion": None})
    assert cloud_results.bring_in(say=cloud.append, wait_s=0, sleep=lambda s: None) is None
    assert "taking too long" in cloud[-1]


def test_github_out_of_reach_means_a_fetch_here(cloud, monkeypatch):
    def broken():
        raise cloud_results.CloudError("gh is not installed")
    monkeypatch.setattr(cloud_results, "today_run", broken)
    assert cloud_results.bring_in(say=cloud.append) is None and "could not reach" in cloud[-1]


# -- the board list the cloud reads ------------------------------------------------------------

def test_the_board_list_goes_encrypted_and_only_when_it_changed(cloud, tmp_path, monkeypatch):
    boards = tmp_path / "boards.csv"
    boards.write_text("source,slug,name\ngreenhouse,acme,Acme\n")
    monkeypatch.setattr(cloud_results, "BOARDS", boards)
    monkeypatch.setattr(cloud_results, "_passphrase", lambda: "secret")
    locked, calls = [], []
    monkeypatch.setattr(cloud_results, "_openssl", lambda args, p: locked.append((args, p)) or
                        open(args[args.index("-out") + 1], "w").write("Salted__…"))

    def gh(*args, **kw):
        calls.append(args[:2])
        if args[:2] == ("release", "view"):
            raise cloud_results.CloudError("release not found")
        return ""
    monkeypatch.setattr(cloud_results, "_gh", gh)
    assert cloud_results.push_boards(say=cloud.append)
    assert locked[0][1] == "secret" and "-salt" in locked[0][0]                # never sent unlocked
    assert calls == [("release", "view"), ("release", "create"), ("release", "upload")]
    assert not cloud_results.push_boards(say=cloud.append)                     # unchanged: not sent again
    boards.write_text(boards.read_text() + "lever,globex,Globex\n")
    calls.clear()
    assert cloud_results.push_boards(say=cloud.append) and ("release", "upload") in calls


# -- the morning: the boards read here only when the cloud has nothing --------------------------

def test_a_scheduled_morning_reads_no_board_here_when_the_cloud_did(tmp_path, monkeypatch):
    from backend.core.daily import render_digest, run_daily
    from data_engineering.db import session as session_mod
    from data_engineering.db.session import init_engine

    class Cfg:
        def get(self, path, default=None):
            return {"resume.base_path": str(resume), "limits.stale_company_warn_days": 0,
                    "excel.path": str(tmp_path / "t.xlsx")}.get(path, default)
    resume = tmp_path / "base_resume.txt"
    resume.write_text("JANE DOE\nSKILLS\nPython, AWS\nEXPERIENCE\n- Built AWS platforms\n")
    init_engine("sqlite:///:memory:")
    monkeypatch.setattr("data_engineering.scraper.feeds.run_feeds", lambda cfg, **kw: [])
    monkeypatch.setattr("data_engineering.scraper.adzuna.credentials", lambda: None)
    monkeypatch.delenv("MAIL_ADDRESS", raising=False)
    read_here = []
    monkeypatch.setattr("data_engineering.scraper.runner.run_scrape", lambda cfg: read_here.append(1))
    monkeypatch.setattr("data_engineering.scraper.runner.sync_seed_companies", lambda cfg: None)
    try:
        monkeypatch.setattr(cloud_results, "set_up", lambda: True)
        monkeypatch.setattr(cloud_results, "bring_in", lambda say=print: {
            "added": 12, "refreshed": 900, "closed": 3, "reopened": 0, "boards": 21000, "run": 42})
        report = run_daily(Cfg(), cloud=True)
        assert read_here == [] and report.stages[0].name == "cloud" and report.jobs_new == 12
        assert "21000 boards scraped by the GitHub run" in render_digest(report, Cfg())
        monkeypatch.setattr(cloud_results, "bring_in", lambda say=print: None)
        run_daily(Cfg(), cloud=True)
        assert read_here == [1]                                                # none today: read here
        # A copy with no GitHub fetch (installed from the Mac app): no cloud stage at all.
        monkeypatch.setattr(cloud_results, "bring_in", lambda say=print: pytest.fail("asked the cloud"))
        monkeypatch.setattr(cloud_results, "set_up", lambda: False)
        report = run_daily(Cfg(), cloud=True)
        assert read_here == [1, 1] and "cloud" not in [s.name for s in report.stages]
    finally:
        session_mod._engine = None
        session_mod._SessionFactory = None
