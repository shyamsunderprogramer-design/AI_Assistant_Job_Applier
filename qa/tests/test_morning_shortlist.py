"""The morning's picks (backend/apply/shortlist.py, 3 Nov 2026, step 1 of the Morning shortlist plan):
the strongest jobs added to the chosen-jobs list by rules that say why each was picked or left out, a job
picked once ever, the person's own choices left alone, and nothing prepared, opened or sent. Made-up jobs
in a throwaway database only."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.apply import execution, runner, shortlist
from backend.core import eligibility
from data_engineering.db import session as session_mod
from data_engineering.db.models import (ApplyArtifact, ApplyEvent, ApplyItem, ApplyRun, Job, ShortlistExclusion,
                                        ShortlistPick)
from data_engineering.db.session import get_session, init_engine

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)     # 6 AM in Chicago
TEXT = "We run Kubernetes and Terraform on AWS. " * 40
MANILA = "Taguig City, National Capital Region (Manila)"

# id: company, board, fit, extra columns. Every job open, posted 3 days ago, in the US, unless it says.
ROWS = {
    1: ("Globex", "greenhouse", 0.95, {}),
    2: ("Initech", "lever", 0.93, {}),
    3: ("Hooli", "adzuna", 0.92, {}),                                            # a listing site
    4: ("Wonka", "jobicy", 0.91, {"employer_url": "https://wonka.example/jobs/4411-platform-engineer",
                                  "employer_url_kind": "posting"}),              # the employer's own, saved
    5: ("Vandelay", "workday", 0.90, {"clearance": "Top Secret", "clearance_active": True}),
    6: ("Umbrella", "greenhouse", 0.89, {"sponsorship": "no"}),                 # will not sponsor
    7: ("Cyberdyne", "ashby", 0.88, {"citizenship": "US citizen or green card"}),  # needs the person's answer
    8: ("Globex", "workday", 0.87, {"title": "Site Reliability Engineer"}),     # Globex's second job
    9: ("Soylent", "greenhouse", 0.86, {"location": MANILA}),                    # abroad
    10: ("Tyrell", "greenhouse", 0.85, {"days": 200}),                          # too old
    11: ("Acme", "greenhouse", 0.84, {}),
    12: ("Massive Dynamic", "greenhouse", 0.83, {}),
    13: ("Stark", "greenhouse", 0.82, {}),
    14: ("Wayne", "greenhouse", 0.79, {}),                                       # under 80%: never looked at
    15: ("Oscorp", "greenhouse", 0.97, {"is_open": False}),                     # closed
    16: ("Pied Piper", "greenhouse", 0.96, {"status": "Applied"}),              # marked applied
}


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


ON = {"shortlist.enabled": True, "limits.max_applications_per_day": 15}


class Person:
    max_per_day = 10
    stop_before_submit = True

    def __init__(self):
        self.authorisation = {"us_citizen": False, "requires_sponsorship": True}
        self.employment = {"current_employer": "Doublene"}
        self.answers, self.job, self.job_answers = {}, None, {}


def _job(n, company, board, fit, extra):
    extra = dict(extra)
    days = extra.pop("days", 3)
    return Job(id=n, source=board, company=company, company_slug=company.lower().replace(" ", "-"),
               external_id=str(n), title=extra.pop("title", "Platform Engineer"),
               location=extra.pop("location", "Remote, US"), description=extra.pop("description", TEXT),
               requirements="", application_url=f"https://{board}.example/{n}", content_hash=f"h{n}",
               is_open=extra.pop("is_open", True), ats_match_score=fit, score_basis="full",
               status=extra.pop("status", "Not Applied"), posted_at=NOW - timedelta(days=days), **extra)


@pytest.fixture
def db(monkeypatch):
    before = (session_mod._engine, session_mod._SessionFactory)
    init_engine("sqlite:///:memory:")
    with get_session() as s:
        for n, (company, board, fit, extra) in ROWS.items():
            s.add(_job(n, company, board, fit, extra))
    from data_engineering.scraper.filters import JobFilter
    from ml.resume.profile import NON_US_PLACES
    monkeypatch.setattr(runner, "searched_places", lambda cfg: JobFilter(exclude_location_keywords=list(NON_US_PLACES)))
    monkeypatch.setattr(runner, "load", lambda path: Person())
    monkeypatch.setattr(runner, "check_ready", lambda applicant: [])
    monkeypatch.setattr(runner, "start_of_today_utc", lambda: NOW.replace(hour=0, minute=0))
    yield
    session_mod._engine, session_mod._SessionFactory = before


def add_jobs(*rows):
    with get_session() as s:
        for n, company, board, fit, extra in rows:
            s.add(_job(n, company, board, fit, extra))


def picked(report):
    return [p["job_id"] for p in report["picked"]]


def reasons(report):
    return {r["job_id"]: r["reason"] for r in report["left_out"]}


def queue():
    with get_session() as s:
        return [(i.job_id, i.state) for i in s.query(ApplyItem).order_by(ApplyItem.position)]


# -- off by default ----------------------------------------------------------------------------------------

def test_switched_off_by_default_it_picks_nothing_and_writes_nothing(db):
    assert shortlist.settings(Cfg())["enabled"] is False and not shortlist.enabled(Cfg())
    assert not shortlist.enabled(Cfg({"shortlist.enabled": "yes"}))             # only a real true switches it on
    report = shortlist.pick(Cfg(), now=NOW)
    assert report["picked"] == [] and "switched off" in report["note"]
    assert queue() == [] and not shortlist.REPORT.exists()


def test_the_morning_run_has_no_shortlist_step_while_it_is_off(db, monkeypatch):
    from backend.core import daily
    import data_engineering.excel.tracker as tracker
    import ml.resume.pipeline as pipeline
    monkeypatch.setattr(pipeline, "score_jobs", lambda cfg: [])
    monkeypatch.setattr(tracker, "export_jobs", lambda cfg, only_new=True: (0, 0, 0))
    monkeypatch.setattr(daily, "stale_companies", lambda days: [])
    off = daily.DailyReport()
    daily._rank_and_gather(off, Cfg(), NOW - timedelta(hours=24))
    assert [s.name for s in off.stages] == ["score", "export", "digest"] and queue() == []
    on = daily.DailyReport()
    daily._rank_and_gather(on, Cfg(ON), NOW - timedelta(hours=24))
    assert [s.name for s in on.stages] == ["score", "export", "digest", "shortlist"] and on.ok
    assert "picked for your chosen jobs" in on.stages[-1].detail and len(queue()) == 5


# -- what is picked, and why -----------------------------------------------------------------------------

def test_the_best_qualified_jobs_are_picked_in_score_order_with_their_reasons(db):
    report = shortlist.pick(Cfg(ON), now=NOW)
    assert picked(report) == [1, 2, 4, 11, 12]                                   # best first, five a day
    first = report["picked"][0]
    assert first["reasons"][0] == "match score 95% (at least 80%)"
    assert shortlist.SPONSORSHIP_UNKNOWN in first["reasons"]                     # unstated: unknown, not a yes
    assert "on greenhouse, the employer's own hiring system" in first["reasons"]
    assert "the employer's own posting, saved on the job's page" in report["picked"][2]["reasons"]
    assert queue() == [(1, "queued"), (2, "queued"), (4, "queued"), (11, "queued"), (12, "queued")]
    with get_session() as s:
        item = s.query(ApplyItem).filter_by(job_id=1).one()
        assert item.decision == eligibility.WITH_WARNINGS                      # its decision is its first step
        said = [e.message for e in s.query(ApplyEvent).filter_by(item_id=item.id).order_by(ApplyEvent.id)]
        assert said[0] == "Added to your chosen jobs" and said[1].startswith("Picked this morning: match score 95%")
        pick = s.query(ShortlistPick).filter_by(job_id=1).one()
        assert pick.day == shortlist.day_of(NOW) and pick.item_id == item.id
        assert pick.rule_version == shortlist.RULE_VERSION
    written = json.loads(shortlist.REPORT.read_text())
    assert picked(written) == picked(report) and written["picked_today"] == 5
    assert written["considered"] == 13                                           # 80%+, open, not marked


def test_every_job_looked_at_and_left_out_says_why(db):
    report = shortlist.pick(Cfg({**ON, "shortlist.per_day": 20}), now=NOW)
    why = reasons(report)
    assert picked(report) == [1, 2, 4, 11, 12, 13]
    assert "listing site (adzuna)" in why[3]
    assert "Top Secret" in why[5] and "from your profile" in why[5]             # never an ineligible job
    assert "will not sponsor a visa" in why[6]
    assert why[7].startswith("needs your confirmation")
    assert why[8] == "one job a company a day: one at Globex is picked today"
    assert why[9] and 9 not in picked(report)                                     # abroad: the search's own rule
    assert "4-month limit" in why[10]                                                # older than the age limit
    for never in (14, 15, 16):                                                    # under 80%, closed, marked
        assert never not in why and never not in picked(report)


def test_an_ineligible_job_is_never_picked_whatever_its_score(db):
    add_jobs((30, "Blackwater", "greenhouse", 0.999, {"citizenship": "US citizen"}),
             (31, "Raytheon", "greenhouse", 0.998, {"clearance": "Secret", "clearance_active": True}))
    report = shortlist.pick(Cfg({**ON, "shortlist.per_day": 50}), now=NOW)
    assert not {5, 6, 30, 31} & set(picked(report))
    assert {5, 6, 30, 31} <= set(reasons(report))


def test_a_job_needing_the_persons_answer_is_picked_only_when_allowed(db):
    report = shortlist.pick(Cfg({**ON, "shortlist.per_day": 50, "shortlist.allow_needs_confirmation": True}), now=NOW)
    seven = next(p for p in report["picked"] if p["job_id"] == 7)
    assert any(r.startswith("Needs your confirmation") for r in seven["reasons"])


def test_a_past_h1b_record_plays_no_part(db, monkeypatch):
    monkeypatch.setattr(eligibility, "sponsor_history", lambda company, country="US": "H-1B")
    report = shortlist.pick(Cfg(ON), now=NOW)
    assert picked(report) == [1, 2, 4, 11, 12]                                   # the same picks
    for p in report["picked"]:
        assert shortlist.SPONSORSHIP_UNKNOWN in p["reasons"]
        assert not any("sponsored" in r for r in p["reasons"])


def test_the_persons_preferences_leave_jobs_out_and_say_so(db):
    add_jobs((40, "Doublene", "greenhouse", 0.99, {}),                          # the current employer
             (41, "Contoso", "greenhouse", 0.99, {"experience_min_years": 12}),
             (42, "Fabrikam", "greenhouse", 0.99, {"workplace": "onsite"}),
             (43, "Northwind", "greenhouse", 0.99, {"job_type": "contract"}),
             (44, "Tailspin", "greenhouse", 0.99, {"salary_max": 90000, "salary_period": "year"}),
             (45, "Adventure", "greenhouse", 0.99, {"description": "Excel and Word. " * 40}),
             (46, "Litware", "greenhouse", 0.99, {"salary_max": 80, "salary_period": "hour"}),   # 166,400 a year
             (47, "Proseware", "greenhouse", 0.99, {}))
    cfg = Cfg({**ON, "shortlist.per_day": 50, "shortlist.required_skills": ["kubernetes", "go"],
               "shortlist.max_years_required": 8, "shortlist.workplaces": ["remote", "hybrid"],
               "shortlist.job_types": ["full-time"], "shortlist.min_salary": 120000,
               "shortlist.exclude_companies": ["Proseware"]})
    report = shortlist.pick(cfg, now=NOW)
    why = reasons(report)
    assert why[40] == "you excluded Doublene" and why[47] == "you excluded Proseware"
    assert why[41] == "it asks for 12+ years; your limit is 8"
    assert why[42].startswith("it is onsite") and why[43].startswith("it is contract")
    assert "under your lowest" in why[44]
    assert why[45].startswith("it mentions none of your required skills")
    assert 46 in picked(report)                                                    # its pay meets the lowest
    assert "mentions kubernetes" in next(p for p in report["picked"] if p["job_id"] == 46)["reasons"]
    assert 1 in picked(report)                     # a posting that does not state a preference is not left out


# -- no duplicates, the person's own choices kept --------------------------------------------------------

def test_jobs_on_the_list_applied_to_or_the_same_elsewhere_are_never_picked(db, monkeypatch, tmp_path):
    execution.add([2])                                                            # chosen by the person
    with get_session() as s:
        s.add(ApplyItem(job_id=11, state="cancelled", position=9))               # cancelled by them before
        s.get(Job, 12).applied_at = (NOW - timedelta(days=30)).replace(tzinfo=None)
    runner.AUDIT_LOG.write_text(json.dumps({"job_id": 99, "company": "Stark", "title": "Platform Engineer"}) + "\n")
    add_jobs((50, "Globex Inc", "lever", 0.995, {}),                             # Globex's job, on another site
             (51, "Initech", "greenhouse", 0.994, {"title": "Data Engineer"}))  # Initech has one on the list
    report = shortlist.pick(Cfg({**ON, "shortlist.per_day": 50}), now=NOW)
    why = reasons(report)
    assert why[2] == "on your chosen list already (queued)"
    assert why[11] == "it was on your chosen list before (cancelled): it is not picked again"
    assert why[12] == "you applied to it already"
    assert why[13] == "you applied to the same job at Stark already (through another site)"
    assert why[51] == "another job at Initech is on your chosen list"
    assert picked(report)[:2] == [50, 4] and why[1] == "the same job at Globex was picked today from another site"


def test_the_persons_own_chosen_jobs_are_left_as_they_are_and_not_counted(db):
    execution.add([13, 14])                                                       # theirs, weak fit or not
    with get_session() as s:
        before = [(i.id, i.job_id, i.state, i.position, i.added_at) for i in s.query(ApplyItem).order_by(ApplyItem.id)]
    report = shortlist.pick(Cfg(ON), now=NOW)
    assert len(report["picked"]) == 5                                            # the five a day are the morning's
    with get_session() as s:
        after = [(i.id, i.job_id, i.state, i.position, i.added_at) for i in s.query(ApplyItem).order_by(ApplyItem.id)]
    assert after[:2] == before and [j for j, _ in queue()][:2] == [13, 14]      # first in line, untouched


def test_a_second_run_and_the_top_up_add_nothing_twice_and_keep_to_the_days_limit(db):
    cfg = Cfg({**ON, "shortlist.per_day": 3})
    assert picked(shortlist.pick(cfg, now=NOW)) == [1, 2, 4]
    again = shortlist.pick(cfg, now=NOW + timedelta(minutes=5))                 # the same morning, run again
    assert again["picked"] == [] and again["picked_today"] == 3
    assert all(r["reason"] == "today's 3 are picked" or "chosen list" in r["reason"] for r in again["left_out"])
    add_jobs((60, "Hal", "greenhouse", 0.999, {}))                               # the night's postings
    top_up = shortlist.pick(cfg, now=NOW + timedelta(hours=3))
    assert top_up["picked"] == [] and len(queue()) == 3                          # the day's three are picked
    tomorrow = shortlist.pick(cfg, now=NOW + timedelta(days=1))
    assert picked(tomorrow) == [60, 11, 12]                                      # a new day: three more, none again
    assert len(queue()) == 6 and len({j for j, _ in queue()}) == 6
    with get_session() as s:
        assert s.query(ShortlistPick).count() == 6


def test_a_job_taken_meanwhile_is_not_added_twice(db):
    shortlist.ensure_tables()                                                     # as pick() does first
    assert shortlist._add(1, ["x"], "2026-11-04", NOW) is not None
    assert shortlist._add(1, ["x"], "2026-11-04", NOW) is None                  # picked already
    execution.add([2])
    assert shortlist._add(2, ["x"], "2026-11-04", NOW) is None                  # on the list from elsewhere
    assert queue() == [(1, "queued"), (2, "queued")]


def test_the_same_jobs_give_the_same_picks(db):
    first = shortlist.pick(Cfg({**ON, "shortlist.per_day": 50}), now=NOW)
    with get_session() as s:
        s.query(ShortlistPick).delete()
        s.query(ApplyEvent).delete()
        s.query(ApplyItem).delete()
    second = shortlist.pick(Cfg({**ON, "shortlist.per_day": 50}), now=NOW)
    assert picked(first) == picked(second) and reasons(first) == reasons(second)


# -- the person's word: removed, not today --------------------------------------------------------------

def test_removed_stays_out_and_not_today_comes_back_to_be_considered(db):
    shortlist.remove(1, "not for me")
    shortlist.defer(2, now=NOW)
    report = shortlist.pick(Cfg(ON), now=NOW)
    why = reasons(report)
    assert why[1] == "you removed it from the morning's picks"
    assert why[2].startswith("you put it off until")
    assert 1 not in picked(report) and 2 not in picked(report)
    tomorrow = shortlist.pick(Cfg(ON), now=NOW + timedelta(days=1))
    assert reasons(tomorrow)[1] == "you removed it from the morning's picks"     # for good
    assert 2 in picked(tomorrow)                                                 # considered again, and fits
    shortlist.restore(1)
    with get_session() as s:
        assert [r.kind for r in s.query(ShortlistExclusion).filter_by(job_id=1)] == ["removed", "restored"]


def test_not_today_promises_nothing(db):
    shortlist.defer(2, now=NOW)
    cfg = Cfg({**ON, "shortlist.per_day": 1})
    assert picked(shortlist.pick(cfg, now=NOW)) == [1]
    assert picked(shortlist.pick(cfg, now=NOW + timedelta(days=1))) == [2]      # the next best, not a reserved place
    shortlist.defer(4, now=NOW + timedelta(days=1))
    with get_session() as s:
        s.get(Job, 4).ats_match_score = 0.5                                        # weaker now: not picked
    assert 4 not in picked(shortlist.pick(cfg, now=NOW + timedelta(days=2)))


# -- nothing prepared, opened or sent ----------------------------------------------------------------------

def test_picking_prepares_no_document_opens_no_form_and_sends_nothing(db, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("the morning's picks must not prepare, open or send anything")
    from backend.apply import anyform, prepare
    for module, name in ((runner, "fill_form"), (anyform, "reach"), (execution, "prepare_these"),
                         (execution, "work"), (execution, "start_run"), (prepare, "prepare")):
        monkeypatch.setattr(module, name, never)
    report = shortlist.pick(Cfg(ON), now=NOW)
    assert len(report["picked"]) == 5
    with get_session() as s:
        assert s.query(ApplyArtifact).count() == 0 and s.query(ApplyRun).count() == 0
        assert {i.state for i in s.query(ApplyItem)} == {"queued"}
    assert not prepare.PREPARED.exists() and not runner.AUDIT_LOG.exists()


def test_with_the_persons_details_incomplete_nothing_is_picked(db, monkeypatch):
    monkeypatch.setattr(runner, "check_ready", lambda applicant: ["your phone number"])
    report = shortlist.pick(Cfg(ON), now=NOW)
    assert report["picked"] == [] and "details are not complete" in report["note"] and queue() == []


# -- speed --------------------------------------------------------------------------------------------------

def test_picking_from_fifteen_thousand_open_jobs_takes_seconds(db):
    with get_session() as s:
        for n in range(1000, 16000):
            s.add(_job(n, f"Company {n}", "greenhouse", 0.60 + (n % 400) / 1000, {}))
    started = time.monotonic()
    report = shortlist.pick(Cfg(ON), now=NOW)
    took = time.monotonic() - started
    print(f"\nusual: picked {len(report['picked'])} of {report['considered']} looked at, "
          f"from 15,016 open jobs, in {took:.2f}s")
    assert len(report["picked"]) == 5 and report["considered"] == 400 and took < 30


def test_even_when_every_job_looked_at_needs_every_check_it_takes_seconds(db):
    with get_session() as s:                       # all 400 best go through every check, and none goes
        for n in range(1000, 16000):
            s.add(_job(n, f"Company {n}", "greenhouse", 0.60 + (n % 400) / 1000,
                       {"citizenship": "US citizen or green card"}))       # passes the run's rules; the decision waits
    started = time.monotonic()
    report = shortlist.pick(Cfg(ON), now=NOW)
    took = time.monotonic() - started
    print(f"\nworst: {report['considered']} looked at, each through every check, in {took:.2f}s")
    assert report["considered"] == 400 and len(report["left_out"]) == 400 and took < 30
    assert all(r["reason"].startswith("needs your confirmation") for r in report["left_out"])


# -- the person's database gains nothing while the shortlist is off (4 Nov 2026) --------------------------

@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """A database in a file, as the person's is, set up the way every process's startup sets it up."""
    before = (session_mod._engine, session_mod._SessionFactory)
    path = tmp_path / "jobs.db"
    init_engine(f"sqlite:///{path}")
    with get_session() as s:
        for n, (company, board, fit, extra) in ROWS.items():
            s.add(_job(n, company, board, fit, extra))
    from data_engineering.scraper.filters import JobFilter
    from ml.resume.profile import NON_US_PLACES
    monkeypatch.setattr(runner, "searched_places", lambda cfg: JobFilter(exclude_location_keywords=list(NON_US_PLACES)))
    monkeypatch.setattr(runner, "load", lambda p: Person())
    monkeypatch.setattr(runner, "check_ready", lambda applicant: [])
    yield path
    session_mod._engine, session_mod._SessionFactory = before


def tables(path):
    import sqlite3
    with sqlite3.connect(path) as con:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_starting_up_switched_off_adds_no_shortlist_table(file_db, monkeypatch):
    from backend.core import daily
    import data_engineering.excel.tracker as tracker
    import ml.resume.pipeline as pipeline
    made = tables(file_db)
    assert {"jobs", "apply_items", "apply_events", "apply_runs", "apply_artifacts", "document_approvals"} <= made
    assert not {"shortlist_picks", "shortlist_exclusions", "shortlist_links"} & made              # startup makes everything else
    init_engine(f"sqlite:///{file_db}")                                        # a second process starting
    monkeypatch.setattr(pipeline, "score_jobs", lambda cfg: [])
    monkeypatch.setattr(tracker, "export_jobs", lambda cfg, only_new=True: (0, 0, 0))
    monkeypatch.setattr(daily, "stale_companies", lambda days: [])
    daily._rank_and_gather(daily.DailyReport(), Cfg(), NOW - timedelta(hours=24))   # a morning run, off
    assert shortlist.pick(Cfg(), now=NOW)["picked"] == []
    assert shortlist.morning_pick(1) is False                                  # the Submit decision asking
    assert tables(file_db) == made and not shortlist.REPORT.exists()          # nothing added


def test_switched_on_the_tables_appear_when_it_first_picks(file_db):
    before = tables(file_db)
    report = shortlist.pick(Cfg(ON), now=NOW)
    after = tables(file_db)
    assert after - before == {"shortlist_picks", "shortlist_exclusions", "shortlist_links"} and len(report["picked"]) == 5
    assert shortlist.morning_pick(1) is True and shortlist.morning_pick(3) is False
    shortlist.pick(Cfg(ON), now=NOW)                                           # created once: nothing twice
    with get_session() as s:
        assert s.query(ShortlistPick).count() == 5
