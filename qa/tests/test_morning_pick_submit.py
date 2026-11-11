"""A job the morning picked is never sent by the app itself (4 Nov 2026): its own rule in the one place a
run decides whether the app may press Submit (execution._one), whatever the person's Submit setting and
whether or not the auto-Submit hold is on. The person still presses Submit on it; every other job is
decided as before. Made-up jobs, a throwaway database, a form filler that opens no page."""

from datetime import datetime

from sqlalchemy import inspect

from backend.apply import execution, runner, shortlist
from data_engineering.db.models import ShortlistPick
from data_engineering.db.session import get_session
from qa.tests.test_eligibility_decisions import (PLAIN, Answers, as_person, no_registers, posted,  # noqa: F401
                                                 recording_form, steps)
from qa.tests.test_execution_queue import Context, db, documents  # noqa: F401

SAYS_IT_SPONSORS = PLAIN + " Visa sponsorship is available."          # nothing in doubt: Eligible


def made_ready(db, monkeypatch, *job_ids):
    """These jobs' forms ask nothing, and their resumes are made, checked and approved by the person."""
    from backend.apply import prepare
    monkeypatch.setattr(prepare, "open_questions", lambda job, applicant: [])
    from qa.tests.test_document_approval import give_word
    from qa.tests.test_prepared_documents import checked_run
    made = db[2] / "checked-resume.pdf"
    made.write_bytes(b"%PDF-1.4 made-up resume")
    for job_id in job_ids:
        checked_run(job_id, made)
        give_word(job_id, "resume", made, "approved")


def picked_by_the_morning(job_id):
    shortlist.ensure_tables()
    with get_session() as s:
        s.add(ShortlistPick(job_id=job_id, day="2026-11-04", picked_at=datetime(2026, 11, 4, 12),
                            rule_version=shortlist.RULE_VERSION, reasons="[]"))


def test_with_automatic_submit_on_and_no_hold_a_morning_pick_still_waits_for_the_persons_submit(
        db, documents, monkeypatch):
    cfg, _, _ = db
    assert not execution.auto_submit_held()                            # the test's own hold file: absent
    posted(1, SAYS_IT_SPONSORS)
    posted(6, SAYS_IT_SPONSORS)
    as_person(monkeypatch, Answers(stop_before_submit=False, requires_sponsorship=True, authorised_to_work=False))
    made_ready(db, monkeypatch, 1, 6)
    picked_by_the_morning(6)
    went = []
    monkeypatch.setattr(runner, "fill_form", recording_form(went))
    execution.add([1, 6])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert sorted(went) == [(1, True), (6, False)]                     # both filled; only the person's own sends itself
    assert "You press Submit on this one: the morning picked it, and the app never sends a morning pick by itself" \
        in steps(6)
    assert not any("morning picked" in s for s in steps(1))


def test_the_hold_on_or_off_changes_nothing_for_a_morning_pick(db, documents, monkeypatch):
    cfg, _, _ = db
    execution.AUTO_SUBMIT_HOLD.write_text("held")                     # the test's own hold, switched on
    posted(6, SAYS_IT_SPONSORS)
    as_person(monkeypatch, Answers(stop_before_submit=False, requires_sponsorship=True, authorised_to_work=False))
    made_ready(db, monkeypatch, 6)
    picked_by_the_morning(6)
    went = []
    monkeypatch.setattr(runner, "fill_form", recording_form(went))
    execution.add([6])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert went == [(6, False)]
    said = steps(6)
    assert any("the morning picked it" in s for s in said)              # its own rule says so, before the hold


def test_with_no_shortlist_tables_every_job_is_decided_as_before(db, documents, monkeypatch):
    cfg, _, _ = db
    posted(1, SAYS_IT_SPONSORS)
    as_person(monkeypatch, Answers(stop_before_submit=False, requires_sponsorship=True, authorised_to_work=False))
    made_ready(db, monkeypatch, 1)
    went = []
    monkeypatch.setattr(runner, "fill_form", recording_form(went))
    execution.add([1])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert went == [(1, True)]
    with get_session() as s:                                          # asking created no table
        assert not inspect(s.get_bind()).has_table("shortlist_picks")


def test_any_doubt_about_a_pick_leaves_submit_to_the_person(db, monkeypatch):
    shortlist.ensure_tables()
    assert shortlist.morning_pick(1) is False
    picked_by_the_morning(1)
    assert shortlist.morning_pick(1) is True

    def broken(*args, **kwargs):
        raise RuntimeError("database busy")
    import sqlalchemy
    monkeypatch.setattr(sqlalchemy, "inspect", broken)
    assert shortlist.morning_pick(2) is True                           # could not tell: the person presses Submit
