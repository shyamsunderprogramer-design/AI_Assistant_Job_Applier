"""Your approval of each document an application sends (2 Nov 2026, backend/apply/approval.py).

Each resume and cover letter made ready for a chosen job is approved or rejected on its own; the word is
kept for exactly that content (job, kind, checksum, when). A document changed afterwards is undecided
again. A rejected one is never attached on any run; the app sends by itself only what was approved; a
document not decided goes as before when the person presses Submit. Made-up jobs and files, a temporary
database and fake forms: no browser, model or network."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.apply import approval, execution, greenhouse, runner
from backend.apply.execution import APPLIED, FAILED
from data_engineering.db.models import ApplyArtifact, ApplyEvent, DocumentApproval
from data_engineering.db.session import get_session
from qa.tests.test_execution_hardening import engine_back, old_database  # noqa: F401
from qa.tests.test_execution_queue import client, db, item_of  # noqa: F401
from qa.tests.test_prepared_documents import Browser, checked_run, dana, desk, recorded, sha  # noqa: F401


def give_word(job_id: int, kind: str, path, word: str) -> None:
    """The person's word on a file, written as the page's press writes it (for tests that start after it)."""
    with get_session() as s:
        s.add(DocumentApproval(job_id=job_id, kind=kind, sha256=sha(path), name=Path(path).name,
                               source="tailored", decision=word))


def steps(job_id) -> list[str]:
    with get_session() as s:
        return [e.message for e in s.query(ApplyEvent).filter(ApplyEvent.job_id == job_id).order_by(ApplyEvent.id)]


def prepared(cfg, job_id) -> dict:
    execution.add([job_id])
    runner.prepare_chosen(cfg)
    return recorded(job_id)


# -- deciding ------------------------------------------------------------------------------------------

def test_each_document_is_approved_or_rejected_on_its_own_with_its_checksum_and_when(db, desk, client):
    cfg, _, _ = db
    c, _ = client
    docs = prepared(cfg, 1)
    item = item_of(1).id
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    assert approval.decide(item, docs["resume"]["id"], "approved")["approval"] == "approved"
    reply = c.post("/apply/chosen/document", json={"item": item, "document": docs["cover_letter"]["id"],
                                                    "decision": "rejected"})
    assert reply.status_code == 200 and reply.get_json()["approval"] == "rejected"
    with get_session() as s:
        rows = [(r.job_id, r.kind, r.sha256, r.decision, r.artifact_id) for r in s.query(DocumentApproval)]
        when = [r.decided_at for r in s.query(DocumentApproval)]
    assert rows == [(1, "resume", docs["resume"]["sha256"], "approved", docs["resume"]["id"]),
                    (1, "cover_letter", docs["cover_letter"]["sha256"], "rejected", docs["cover_letter"]["id"])]
    assert all(before <= w <= datetime.now(timezone.utc).replace(tzinfo=None) for w in when)
    assert f"You approved its resume {Path(docs['resume']['path']).name} (checksum {docs['resume']['sha256'][:12]})" in steps(1)
    view = next(i for i in c.get("/apply/chosen").get_json()["items"] if i["job_id"] == 1)
    assert [(d["kind"], d["approval"]) for d in view["documents"]] == [("resume", "approved"),
                                                                       ("cover_letter", "rejected")]
    # The file opens to be read before deciding; nothing else does.
    assert c.get(f"/apply/chosen/document/{docs['resume']['id']}").data == Path(docs["resume"]["path"]).read_bytes()
    assert c.get("/apply/chosen/document/999").status_code == 404


@pytest.mark.parametrize("body", [{}, {"item": 1, "document": 1}, {"item": 1, "document": True, "decision": "approved"},
                                  {"item": 1, "document": 1, "decision": "maybe"}])
def test_the_press_must_say_which_document_and_approved_or_rejected(db, client, body):
    c, _ = client
    assert c.post("/apply/chosen/document", json=body).status_code == 400


def test_a_document_changed_since_it_was_made_ready_cannot_be_decided(db, desk, client):
    cfg, _, _ = db
    c, _ = client
    docs = prepared(cfg, 1)
    made = Path(docs["resume"]["path"])
    made.write_bytes(made.read_bytes() + b" -- changed by hand")
    reply = c.post("/apply/chosen/document", json={"item": item_of(1).id, "document": docs["resume"]["id"],
                                                    "decision": "approved"})
    assert reply.status_code == 409 and "has changed or is gone since it was made ready" in reply.get_json()["error"]
    desk.write(1, "letter", "%PDF-1.4 the letter as edited on the job's page")      # its own file changed
    with pytest.raises(approval.ApprovalError, match="changed or is gone"):
        approval.decide(item_of(1).id, docs["cover_letter"]["id"], "approved")
    with get_session() as s:
        assert s.query(DocumentApproval).count() == 0                          # nothing marked approved


def test_only_a_document_made_ready_and_not_yet_sent_can_be_decided(db, desk):
    cfg, _, _ = db
    docs = prepared(cfg, 1)
    with get_session() as s:
        s.get(ApplyArtifact, docs["resume"]["id"]).state = "sent"
    with pytest.raises(approval.ApprovalError, match="no longer the one its form would be given"):
        approval.decide(item_of(1).id, docs["resume"]["id"], "approved")
    with pytest.raises(approval.ApprovalError, match="not one made ready for this job"):
        approval.decide(item_of(1).id + 1, docs["cover_letter"]["id"], "approved")


# -- a changed document is undecided again ---------------------------------------------------------------

def test_an_approval_holds_for_exactly_that_content_and_not_for_a_changed_one(db, desk, client):
    cfg, _, _ = db
    c, _ = client
    docs = prepared(cfg, 1)
    approval.decide(item_of(1).id, docs["resume"]["id"], "approved")
    assert approval.unapproved(1, "resume", docs["resume"]["path"]) is None
    desk.write(1, "resume", "%PDF-1.4 tailored again on the job's page")       # a new version of it
    assert approval.unapproved(1, "resume", desk.own(1, "resume")) == "you have not approved its resume yet"
    runner.run(cfg, chosen=True, browser_factory=Browser)                       # the one made ready is stale now
    execution.retry(item_of(1).id)
    runner.prepare_chosen(cfg)
    view = next(i for i in c.get("/apply/chosen").get_json()["items"] if i["job_id"] == 1)
    resume = next(d for d in view["documents"] if d["kind"] == "resume")
    assert resume["sha256"] == sha(desk.own(1, "resume"))
    assert (resume["approval"], resume["approval_earlier"]) == ("", True)        # "changed since your last word"


# -- a rejected document never goes ----------------------------------------------------------------------

def test_a_rejected_document_is_never_sent_and_the_job_waits_for_a_new_one(db, desk):
    cfg, _, _ = db
    docs = prepared(cfg, 1)
    approval.decide(item_of(1).id, docs["cover_letter"]["id"], "rejected")
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert Browser.forms == [] and item_of(1).state == FAILED                  # no form opened
    assert item_of(1).outcome.startswith("Not sent: you rejected its cover letter")
    execution.retry(item_of(1).id)                                             # the same letter: still not sent
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert Browser.forms == [] and item_of(1).state == FAILED
    desk.write(1, "letter", "%PDF-1.4 a new letter written on the job's page")  # a new one: undecided, it goes
    execution.retry(item_of(1).id)
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert item_of(1).state == APPLIED
    assert sha(Browser.forms[0].files["input#cover_letter"]) == sha(desk.own(1, "letter"))


@pytest.fixture
def real_prepare(db, monkeypatch, tmp_path):
    """The real prepare_documents (every run's way to its files) with a made-up resume and letter."""
    folder = tmp_path / "attempt"
    folder.mkdir()
    monkeypatch.setattr(greenhouse, "_evidence_dir", lambda job_id: folder)
    monkeypatch.setattr("ml.resume.packet.build_packet", lambda cfg, job: None)
    monkeypatch.setattr(runner, "write_letter_if_missing", lambda cfg, job, head="": True)
    resume, letter = tmp_path / "resume.pdf", tmp_path / "letter.pdf"
    resume.write_bytes(b"%PDF-1.4 the tailored resume")
    letter.write_bytes(b"%PDF-1.4 the letter")
    chosen = {"resume": (resume, "tailored")}
    monkeypatch.setattr(runner, "choose_resume", lambda cfg, job, tailor=True: chosen["resume"])
    monkeypatch.setattr(runner, "cover_letter", lambda cfg, job, evidence: letter)
    monkeypatch.setattr(runner, "_letter_origin", lambda cfg, job: letter)
    return chosen, resume, letter


def _job(job_id=1):
    from data_engineering.db.models import Job
    with get_session() as s:
        job = s.get(Job, job_id)
        s.expunge(job)
        return job


def test_every_run_refuses_a_rejected_document_and_takes_one_not_decided_as_before(db, real_prepare):
    cfg, _, _ = db
    chosen, resume, letter = real_prepare
    ready = runner.prepare_documents(cfg, _job(), dana(), "Globex", tailor=False)
    assert isinstance(ready, runner.Prepared) and sha(ready.kept) == sha(resume)   # not decided: goes as before
    give_word(1, "resume", resume, "rejected")
    assert runner.prepare_documents(cfg, _job(), dana(), "Globex", tailor=False) == (
        "you rejected its resume (resume.pdf); make a new one on its job page")
    give_word(1, "resume", resume, "approved")                                  # the latest word stands
    give_word(1, "cover_letter", letter, "rejected")
    assert runner.prepare_documents(cfg, _job(), dana(), "Globex", tailor=False) == (
        "you rejected its cover letter (letter.pdf); make a new one on its job page")


def test_a_rejected_selection_from_your_own_lines_holds_though_it_is_drawn_afresh(db, real_prepare, tmp_path):
    cfg, _, _ = db
    chosen, _, _ = real_prepare
    first = tmp_path / "selected-1.pdf"
    first.write_bytes(b"%PDF-1.4 your own lines, drawn on Monday")
    with get_session() as s:
        s.add(DocumentApproval(job_id=1, kind="resume", sha256=sha(first), name=first.name,
                               source="your resume selected for this job — tailoring off", decision="rejected"))
    again = tmp_path / "selected-2.pdf"
    again.write_bytes(b"%PDF-1.4 your own lines, drawn on Tuesday")             # another checksum, same words
    chosen["resume"] = (again, "your resume selected for this job — tailoring off")
    assert runner.prepare_documents(cfg, _job(), dana(), "Globex", tailor=False) == (
        "you rejected the resume selected from your own lines for it; tailor one on its job page")
    tailored = tmp_path / "tailored.pdf"                                         # a tailored one is not held back
    tailored.write_bytes(b"%PDF-1.4 tailored at last")
    chosen["resume"] = (tailored, "tailored")
    assert isinstance(runner.prepare_documents(cfg, _job(), dana(), "Globex", tailor=False), runner.Prepared)


# -- sent by the app itself only when approved ------------------------------------------------------------

def test_the_app_sends_by_itself_only_a_resume_you_approved(db, tmp_path):
    resume = tmp_path / "Dana_Example_Resume_Globex.pdf"
    resume.write_bytes(b"%PDF-1.4 the resume made for job 1")
    checked_run(1, resume)                                                     # its own checks all pass
    assert execution.documents_unready(1, resume, None) == "you have not approved its resume yet"
    give_word(1, "resume", resume, "rejected")
    assert execution.documents_unready(1, resume, None) == "you rejected its resume"
    give_word(1, "resume", resume, "approved")
    assert execution.documents_unready(1, resume, None) is None


def test_a_run_leaves_submit_to_you_until_you_approve_its_resume(db, desk, monkeypatch):
    cfg, _, _ = db
    went = []

    def fill_form(cfg, page, job, applicant, head, ready, *, auto, wait_s):
        went.append((job.id, auto))
        return runner.Outcome(job.id, job.company, job.title, "skipped")
    monkeypatch.setattr(runner, "fill_form", fill_form)
    monkeypatch.setattr(runner, "load", lambda path: dana(stop_before_submit=False))
    from backend.apply import prepare
    monkeypatch.setattr(prepare, "open_questions", lambda job, applicant: [])
    desk.letters, desk.checked = False, True                                   # vouched for by its own checks
    prepared(cfg, 1)
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert went == [(1, False)]
    assert "You press Submit on this one: you have not approved its resume yet" in steps(1)
    execution.retry(item_of(1).id)
    approval.decide(item_of(1).id, recorded(1)["resume"]["id"], "approved")
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert went == [(1, False), (1, True)]


def test_a_database_from_before_gains_the_approvals_table_and_keeps_every_row(tmp_path, engine_back):
    from sqlalchemy import inspect
    from sqlalchemy.schema import CreateTable

    from data_engineering.db import session as session_mod
    from data_engineering.db.models import ApplyEvent, ApplyRun
    from data_engineering.db.session import init_engine
    url = old_database(tmp_path / "before.db", apply_tables_sql=(
        CreateTable(ApplyRun.__table__), CreateTable(ApplyEvent.__table__)))
    init_engine(url)
    assert "document_approvals" in inspect(session_mod._engine).get_table_names()
