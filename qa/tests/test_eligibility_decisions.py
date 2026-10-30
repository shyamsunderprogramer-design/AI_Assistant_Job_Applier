"""Auto-apply Phase 3, step 1 (26 Oct 2026): each chosen job's eligibility decision -- ELIGIBLE,
ELIGIBLE_WITH_WARNINGS, NEEDS_INFORMATION, INELIGIBLE or UNVERIFIED -- read from the eligibility rules
the app already keeps (backend/core/eligibility.py, runner.blocked_reason), with the posting's own
words. What it changes in a run: a job waiting for the person's answer is set aside while the rest go,
and only an ELIGIBLE one may be sent by the app itself. Made-up postings, a temporary database and a
fake form: no browser, model or network (conftest refuses them)."""

import itertools
import json
import os
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateTable

from backend.apply import chosen, execution, runner
from backend.apply.execution import APPLIED, QUEUED, VALIDATING
from backend.core import eligibility
from backend.core.eligibility import (ELIGIBLE, INELIGIBLE, LEVELS, NEEDS_INFORMATION, RULE_VERSION, UNVERIFIED,
                                      WITH_WARNINGS, decide, eligible, held_level)
from data_engineering.db.models import ApplyEvent, ApplyItem, ApplyRun, Job
from data_engineering.db.session import get_session, init_engine
from qa.tests.test_execution_hardening import engine_back, old_database  # noqa: F401
from qa.tests.test_execution_queue import Context, client, db, documents, item_of  # noqa: F401

CLEARED = ("Platform Engineer. You must hold an active TS/SCI clearance with polygraph. "
           "US citizenship is required. We are unable to sponsor visas.")
PLAIN = "We build warehouse robots. Python, SQL and Kubernetes. Hybrid in Austin."
NOT_SAID = {"authorised_to_work": True, "requires_sponsorship": False}       # the two a run insists on


def posting(words, *, manual=None, **facts):
    """A job as the app keeps one: its facts read from its words, as the scrape reads them."""
    read = eligibility.parse("Platform Engineer", words, "")
    read.update(facts)
    return SimpleNamespace(id=7, title="Platform Engineer", company="Example Robotics", description=words,
                           requirements="", eligibility_manual=manual, **read)


@pytest.fixture(autouse=True)
def no_registers(monkeypatch):
    """The companies' sponsorship record is never read from the person's own data/companies.db here."""
    monkeypatch.setattr(eligibility, "sponsor_history", lambda company, country="US": None)


# -- the five decisions --------------------------------------------------------------------------

def test_a_posting_that_asks_nothing_the_profile_rules_out_is_eligible_and_may_go_by_itself():
    d = decide(posting(PLAIN), {**NOT_SAID, "us_citizen": False})
    assert (d.decision, d.label, d.goes, d.sends_itself) == (ELIGIBLE, "Eligible", True, True)
    # Only who may take the job is decided: the words say so, never "nothing rules you out" (26 Oct 2026).
    assert d.reasons == ["no clearance, citizenship or visa requirement in the posting stands against your profile; "
                         "its skills, experience and qualifications are not part of this check"]
    assert d.evidence == d.unknowns == d.blocking_requirements == d.warnings == []
    assert d.rule_version == RULE_VERSION and datetime.fromisoformat(d.evaluated_at).utcoffset().total_seconds() == 0
    assert d.as_dict()["label"] == "Eligible" and json.loads(json.dumps(d.as_dict()))["decision"] == ELIGIBLE


def test_a_stated_requirement_against_a_stated_answer_rules_it_out_in_the_postings_own_words():
    d = decide(posting(CLEARED), {**NOT_SAID, "us_citizen": False})
    assert d.decision == INELIGIBLE and not d.goes and not d.sends_itself
    assert d.blocking_requirements == [
        "needs an active TS/SCI clearance, which only US citizens hold (the employer's requirement, against your profile)",
        "US citizens only (the employer's requirement, against your profile)"]
    assert d.reasons == d.blocking_requirements
    assert d.evidence == ["Clearance: “You must hold an active TS/SCI clearance with polygraph.”",
                          "Polygraph: “You must hold an active TS/SCI clearance with polygraph.”",
                          "Citizenship: “US citizenship is required.”",
                          "Sponsorship: “We are unable to sponsor visas.”"]


def test_an_unanswered_question_is_never_taken_for_a_yes_or_a_no():
    job = posting(CLEARED)
    d = decide(job, NOT_SAID)                               # citizenship and clearance left unanswered
    assert d.decision == NEEDS_INFORMATION and not d.goes and d.label == "Needs your answer"
    assert len(d.unknowns) == 2 and all("answer it in Settings → Your details" in u for u in d.unknowns)
    assert d.unknowns[0].startswith("needs an active TS/SCI clearance, and your profile doesn't say whether you hold one")
    assert d.blocking_requirements == [] and d.reasons == d.unknowns
    assert eligible(job, NOT_SAID) == (True, [])            # as ever: an unanswered question hides no job
    held = {**NOT_SAID, "us_citizen": True}
    assert decide(job, {**held, "security_clearance": "TS/SCI with polygraph"}).decision == ELIGIBLE
    poly = decide(job, {**held, "security_clearance": "TS/SCI"})
    assert (poly.decision, poly.warnings) == (WITH_WARNINGS, ["a polygraph is part of its clearance"])
    assert not poly.sends_itself and poly.goes
    no_visa = decide(job, {"us_citizen": True, "security_clearance": "TS/SCI with polygraph"})
    assert no_visa.decision == NEEDS_INFORMATION and "whether you need sponsorship" in no_visa.unknowns[0]


def test_a_visa_the_posting_never_mentions_is_a_warning_and_the_companys_record_is_never_proof(monkeypatch):
    needs_visa = {"authorised_to_work": False, "requires_sponsorship": True, "us_citizen": False}
    d = decide(posting(PLAIN), needs_visa)
    assert (d.decision, d.warnings) == (WITH_WARNINGS, ["doesn't say whether it sponsors a visa, and you need one"])
    monkeypatch.setattr(eligibility, "sponsor_history", lambda company, country="US": "H-1B")
    d = decide(posting(PLAIN), needs_visa)
    assert d.decision == WITH_WARNINGS and not d.sends_itself              # a record of the past, not of this role
    assert d.warnings[1] == ("Example Robotics has sponsored H-1B visas before (official records); "
                             "that is no promise this role will")
    refused = decide(posting(PLAIN + " We do not sponsor visas."), needs_visa)
    assert refused.decision == INELIGIBLE                                   # the posting's word beats the record
    offered = decide(posting(PLAIN + " Visa sponsorship is available."), needs_visa)
    assert offered.decision == ELIGIBLE and offered.evidence == ["Sponsorship: “Visa sponsorship is available.”"]


def test_what_could_not_be_read_is_unverified_and_never_sent_by_itself():
    empty = SimpleNamespace(id=8, title="Platform Engineer", company="Example Robotics", description="",
                            requirements=None, eligibility_manual=None, clearance=None, clearance_active=None,
                            polygraph=None, citizenship=None, sponsorship=None)
    d = decide(empty, NOT_SAID)
    assert d.decision == UNVERIFIED and d.goes and not d.sends_itself and d.label == "Unverified"
    assert "the posting's words aren't saved" in d.unknowns[0]
    # Saved before the posting was read for it: its words say citizens only, its saved details nothing.
    stale = posting(PLAIN + " Must be a US citizen.", citizenship=None)
    d = decide(stale, {**NOT_SAID, "us_citizen": True})
    assert d.decision == UNVERIFIED and d.unknowns == [
        "its posting states US citizen only, which the job's saved details leave out: check them on the job's page"]
    # The person's own correction on the job's page stands, and is named as theirs.
    fixed = posting(PLAIN + " Must be a US citizen.", citizenship=None, manual=True)
    assert decide(fixed, {**NOT_SAID, "us_citizen": True}).decision == ELIGIBLE
    named = posting(CLEARED, manual=True)
    assert decide(named, {**NOT_SAID, "us_citizen": False}).evidence[0] == "Clearance: as you set it on the job's page"


def test_legacy_rules_stay_equivalent_except_unanswered_green_card_is_unknown():
    def before(job, authorisation):                       # eligible() as it was until 26 Oct 2026
        reasons = []
        level = job.clearance
        if level and job.clearance_active:
            named = "security" if level == "Clearance" else level
            held = authorisation.get("security_clearance")
            if held is not None:
                need = LEVELS.index(level) + 1 if level in LEVELS else 1
                if held_level(held) < need:
                    reasons.append(f"needs an active {named} clearance")
            elif authorisation.get("us_citizen") is False:
                reasons.append(f"needs an active {named} clearance, which only US citizens hold")
        citizen = authorisation.get("us_citizen")
        if job.citizenship == "US citizen" and citizen is False:
            reasons.append("US citizens only")
        elif job.citizenship == "US citizen or green card" and citizen is False and not authorisation.get("green_card"):
            reasons.append("US citizens or green card holders only")
        if job.sponsorship == "no" and authorisation.get("requires_sponsorship") is True:
            reasons.append("will not sponsor a visa")
        return not reasons, reasons

    jobs = [SimpleNamespace(clearance=c, clearance_active=a, polygraph=p, citizenship=z, sponsorship=s)
            for c, a, p, z, s in itertools.product((None, "Clearance", *LEVELS), (None, False, True), (None, True),
                                                   (None, "US citizen", "US citizen or green card"), (None, "no", "yes"))]
    people = [{"security_clearance": c, "us_citizen": u, "green_card": g, "requires_sponsorship": r}
              for c, u, g, r in itertools.product((None, "None", "Secret", "TS/SCI", "TS/SCI with polygraph"),
                                                  (None, True, False), (None, True, False), (None, True, False))]
    for job, person in itertools.product(jobs, people):
        expected, reasons = before(job, person)
        if job.citizenship == "US citizen or green card" and person["us_citizen"] is False and person["green_card"] is None:
            reasons.remove("US citizens or green card holders only")
            expected = not reasons
        assert eligible(job, person) == (expected, reasons), (vars(job), person)
    assert len(jobs) * len(people) == 43_740


@pytest.mark.parametrize("citizen,green_card,expected", [
    (False, None, NEEDS_INFORMATION), (None, False, NEEDS_INFORMATION),
    (None, None, NEEDS_INFORMATION), (False, False, INELIGIBLE),
    (False, True, ELIGIBLE), (True, None, ELIGIBLE),
])
def test_citizenship_or_green_card_requires_explicit_answers(citizen, green_card, expected):
    job = posting("Must be a US citizen or permanent resident.")
    d = decide(job, {**NOT_SAID, "us_citizen": citizen, "green_card": green_card})
    assert d.decision == expected
    if expected == NEEDS_INFORMATION:
        assert d.unknowns and not d.blocking_requirements and not d.sends_itself and not d.goes


# -- on the Jobs page's review, and on the list ----------------------------------------------------

class Answers:
    """The person, with the answers a test gives them."""
    max_per_day = 10

    def __init__(self, stop_before_submit=True, **authorisation):
        self.stop_before_submit = stop_before_submit
        self.authorisation = {**NOT_SAID, **authorisation}
        self.answers, self.job, self.job_answers = {}, None, {}


def posted(job_id, words, **facts):
    """Give a made-up job of the database these words, and the facts the scrape reads from them."""
    read = eligibility.parse("Platform Engineer", words, "")
    read.update(facts)
    with get_session() as s:
        job = s.get(Job, job_id)
        job.description = words
        for column, value in read.items():
            setattr(job, column, value)


def as_person(monkeypatch, person):
    monkeypatch.setattr(runner, "load", lambda path: person)


def steps(job_id):
    with get_session() as s:
        return [e.message for e in s.query(ApplyEvent).filter(ApplyEvent.job_id == job_id).order_by(ApplyEvent.id)]


def test_the_review_shows_each_decision_and_one_waiting_for_an_answer_takes_no_ones_turn(db, monkeypatch):
    cfg, _, _ = db
    posted(1, PLAIN + " Must be a US citizen.")           # Globex's better fit asks what is not answered
    as_person(monkeypatch, Answers())
    result = chosen.check(cfg, [1, 4, 2, 3])
    rows = {r["id"]: r for r in result["jobs"]}
    assert rows[1]["state"] == "needs" and rows[1]["reason"].startswith("US citizens only, and your profile doesn't say")
    assert rows[1]["decision"]["decision"] == NEEDS_INFORMATION
    assert rows[4]["state"] == "ready" and rows[4]["decision"]["decision"] == ELIGIBLE    # Globex's turn is its own
    assert rows[3]["state"] == "ready" and rows[3]["decision"]["decision"] == ELIGIBLE      # Jobvite: assisted
    assert [r["id"] for r in result["jobs"]] == [3, 2, 4, 1]                                 # best fit first
    assert (result["ready"], result["later"], result["needs"], result["blocked"]) == (3, 0, 1, 0)


def test_jobs_added_to_the_list_carry_their_decision_and_it_is_written_down_as_a_step(db, monkeypatch):
    cfg, _, _ = db
    posted(1, PLAIN + " Must be a US citizen.")
    as_person(monkeypatch, Answers())
    chosen.select([1, 2, 3])
    moved = chosen.transfer(cfg)
    assert moved["added"] == 3 and moved["kept"] == []            # 1 waits on the list for its answer
    assert item_of(1).decision == NEEDS_INFORMATION and item_of(2).decision == ELIGIBLE
    detail = json.loads(item_of(1).decision_detail)
    assert detail["evidence"] == ["Citizenship: “Must be a US citizen.”"] and detail["rule_version"] == RULE_VERSION
    assert steps(1) == ["Added to your chosen jobs",
                        "Eligibility: Needs your answer — US citizens only, and your profile doesn't say whether you "
                        "are one: answer it in Settings → Your details"]
    execution.view(cfg)
    execution.view(cfg)                                    # looked at again, nothing changed: nothing written
    assert len(steps(1)) == 2


def test_a_run_sets_aside_the_job_waiting_for_an_answer_goes_on_with_the_rest_and_takes_it_once_answered(
        db, documents, monkeypatch):
    cfg, _, _ = db
    posted(1, PLAIN + " Must be a US citizen.")
    as_person(monkeypatch, Answers())
    went = []
    monkeypatch.setattr(runner, "fill_form", recording_form(went))
    execution.add([1, 2])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert went == [(2, False)] and item_of(2).state == APPLIED
    assert item_of(1).state == QUEUED and item_of(1).attempts == 0                 # never taken, never skipped
    with get_session() as s:
        run = s.query(ApplyRun).order_by(ApplyRun.id.desc()).first()
    assert run.note == "Nothing more can go now: 1 wait for your answer (each says what it needs)."
    # The person answers; the list sees it at its next look, and the next run takes the job.
    as_person(monkeypatch, Answers(us_citizen=True))
    shown = {i["job_id"]: i for i in execution.view(cfg)["items"]}
    assert shown[1]["decision"]["decision"] == ELIGIBLE and shown[1]["turn"] == "ready"
    assert steps(1)[-1].startswith("Eligibility: Eligible — no clearance, citizenship or visa requirement")
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert went[-1] == (1, False) and item_of(1).state == APPLIED


def recording_form(went):
    """A form filler that never opens a page: it notes whether it was let to press Submit itself."""
    def fill_form(cfg, page, job, applicant, head, ready, *, auto, wait_s):
        went.append((job.id, auto))
        runner.record_application(job.id, {"company": job.company, "title": job.title, "how": "auto" if auto else "you"})
        return runner.Outcome(job.id, job.company, job.title, "submitted", how="auto" if auto else "you")
    return fill_form


def test_only_an_eligible_job_is_sent_by_the_app_itself_and_each_check_before_it_goes_is_written_down(
        db, documents, monkeypatch):
    cfg, _, _ = db
    posted(1, PLAIN + " Visa sponsorship is available.")             # says it sponsors: nothing in doubt
    posted(6, PLAIN)                                                  # says nothing about a visa
    as_person(monkeypatch, Answers(stop_before_submit=False, requires_sponsorship=True, authorised_to_work=False))
    from backend.apply import prepare                                 # the boards answer: their forms ask nothing
    monkeypatch.setattr(prepare, "open_questions", lambda job, applicant: [])
    from qa.tests.test_prepared_documents import checked_run          # their resumes made and checked
    made = cfg_dir = db[2] / "checked-resume.pdf"
    made.write_bytes(b"%PDF-1.4 made-up resume")
    for job_id in (1, 6):
        checked_run(job_id, cfg_dir)
    went = []
    monkeypatch.setattr(runner, "fill_form", recording_form(went))
    execution.add([1, 6, 2])                                          # two Greenhouse forms, and JazzHR's
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert sorted(went) == [(1, True), (2, False), (6, False)]        # the preference stands; only 1 may use it
    assert [s for s in steps(2) if s.startswith("Eligibility")] and not any(s.startswith("You press") for s in steps(2))
    assert [s for s in steps(6) if s.startswith(("Eligibility", "You press"))] == [
        "Eligibility: Eligible, with warnings — doesn't say whether it sponsors a visa, and you need one",
        "You press Submit on this one: it is eligible, with warnings (doesn't say whether it sponsors a visa, "
        "and you need one)"]
    assert [s for s in steps(1) if s.startswith("Eligibility")] == [
        "Eligibility: Eligible — no clearance, citizenship or visa requirement in the posting stands against "
        "your profile; its skills, experience and qualifications are not part of this check"]
    assert not any(s.startswith("You press") for s in steps(1))


def test_a_job_ruled_out_since_it_was_added_is_skipped_as_before_with_its_reason(db, documents, monkeypatch):
    cfg, _, _ = db
    as_person(monkeypatch, Answers())
    execution.add([1])
    posted(1, PLAIN + " Must be a US citizen.")
    as_person(monkeypatch, Answers(us_citizen=False))                 # answered no meanwhile
    monkeypatch.setattr(runner, "fill_form", recording_form([]))
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert item_of(1).state == "skipped" and item_of(1).outcome == "Can't go: US citizens only (from your profile)."


def test_an_answer_taken_back_after_a_job_was_picked_puts_it_back_to_wait(db, documents, monkeypatch):
    cfg, _, _ = db
    posted(1, PLAIN + " Must be a US citizen.")
    execution.add([1])
    item = item_of(1)
    run = execution.start_run(os.getpid())
    execution.move(item.id, VALIDATING, run_id=run.id)                # picked while it was answered yes
    shown = execution._one(cfg, Answers(), run.id, item.id, runner.searched_places(cfg),   # now unanswered
                           {"context": None, "close": lambda: None, "factory": Context}, tailor=False, wait_s=1)
    assert shown is None and item_of(1).state == QUEUED
    assert item_of(1).note.startswith("Waits for your answer: US citizens only, and your profile doesn't say")
    assert documents == []                                            # nothing was prepared for it


def test_a_job_that_comes_up_twice_in_one_run_stops_the_run_rather_than_going_round_for_ever(
        db, documents, monkeypatch):
    """Were the run's choice and its check before a job goes ever to disagree -- here the choice is
    made blind to unanswered questions -- the job sent back to wait would be chosen again and again."""
    cfg, _, _ = db
    posted(1, PLAIN + " Must be a US citizen.")
    as_person(monkeypatch, Answers())
    monkeypatch.setattr(runner, "needs_answer", lambda job, applicant: False)
    monkeypatch.setattr(runner, "fill_form", recording_form([]))
    execution.add([1])
    runner.run(cfg, chosen=True, browser_factory=Context)
    with get_session() as s:
        run = s.query(ApplyRun).order_by(ApplyRun.id.desc()).first()
    assert (run.state, run.note) == ("stopped", "Stopped: the same job came up twice in this run, so nothing more was tried.")
    assert item_of(1).state == QUEUED and item_of(1).attempts == 2 and documents == []


def test_the_list_and_the_review_pages_carry_the_decision(client, monkeypatch):
    c, _ = client
    posted(1, PLAIN + " Must be a US citizen.")
    as_person(monkeypatch, Answers())
    execution.add([2])
    chosen.select([1])
    review = c.get("/selection/review").get_json()
    assert review["needs"] == 1 and review["jobs"][0]["decision"]["label"] == "Needs your answer"
    view = c.get("/apply/chosen").get_json()
    assert view["items"][0]["decision"]["decision"] == ELIGIBLE
    assert view["items"][0]["decision"]["evaluated_at"] and view["items"][0]["decision"]["rule_version"] == RULE_VERSION
    # Both pages say what a decision covers, and that skills and experience are not part of it.
    for page in ("/", "/apply"):
        html = c.get(page).get_data(as_text=True)
        assert "not skills, experience or qualifications: the Fit score weighs those." in html, page


# -- the database ----------------------------------------------------------------------------------

def test_a_list_from_before_the_decisions_gains_their_columns_and_keeps_every_row(tmp_path, engine_back):
    phase_two = ("CREATE TABLE apply_items (id INTEGER PRIMARY KEY, job_id INTEGER, state VARCHAR(24), "
                 "position INTEGER, attempts INTEGER, run_id INTEGER, note TEXT, action TEXT, outcome TEXT, "
                 "hidden BOOLEAN, added_at DATETIME, updated_at DATETIME, finished_at DATETIME)")
    when = "'2026-10-25 20:00:00.000000'"
    url = old_database(tmp_path / "phase2.db", apply_tables_sql=(
        phase_two, CreateTable(ApplyRun.__table__), CreateTable(ApplyEvent.__table__),
        f"INSERT INTO apply_items (id, job_id, state, position, attempts, note, hidden, added_at, updated_at) VALUES "
        f"(1, 1, 'queued', 1, 0, 'Waiting its turn', 0, {when}, {when}), "
        f"(2, 2, 'applied', 2, 1, NULL, 1, {when}, {when})"))
    init_engine(url)
    from data_engineering.db import session as session_mod
    columns = {c["name"] for c in inspect(session_mod._engine).get_columns("apply_items")}
    assert {"decision", "decision_detail"} <= columns
    with get_session() as s:
        rows = {i.id: (i.job_id, i.state, i.note, i.hidden, i.decision) for i in s.query(ApplyItem)}
    assert rows == {1: (1, QUEUED, "Waiting its turn", False, None), 2: (2, APPLIED, None, True, None)}
    assert execution.decided(1, decide(posting(PLAIN), NOT_SAID)) is True
    with session_mod._engine.connect() as conn:
        assert conn.execute(text("SELECT decision FROM apply_items WHERE id = 1")).scalar() == ELIGIBLE
