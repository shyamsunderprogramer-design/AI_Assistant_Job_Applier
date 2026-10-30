"""Auto-apply Phase 3, step 5 (26 Oct 2026): history linked. An application sent from the chosen jobs'
list says where it came from -- its entry, run and attempt -- and exactly which documents went, by
checksum, in its line in data/applications.jsonl and on its Applied page; the person's own "it went"
is linked the same way; there is one record however many say it went, and its first day stands; and
the list, the job, the Applied page and the history all agree. Made-up jobs and documents, a fake
Greenhouse form, a temporary database: no browser, model or network."""

import json
from datetime import timedelta

import pytest

from backend.apply import execution, greenhouse, runner, tracker
from backend.apply.execution import APPLIED, PENDING
from data_engineering.db.models import ApplyArtifact, ApplyRun, Job
from data_engineering.db.session import get_session
from qa.tests.test_execution_queue import client, db, item_of  # noqa: F401
from qa.tests.test_prepared_documents import Browser, dana, desk, sha  # noqa: F401


def audit(job_id) -> list[dict]:
    log = runner.AUDIT_LOG
    lines = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [e for e in lines if e["job_id"] == job_id]


def runs() -> list[int]:
    with get_session() as s:
        return [r.id for r in s.query(ApplyRun).order_by(ApplyRun.id)]


def artifacts(job_id) -> dict:
    with get_session() as s:
        return {(a.kind, a.state): a.sha256 for a in s.query(ApplyArtifact).filter(ApplyArtifact.job_id == job_id)}


def applied_at(job_id):
    with get_session() as s:
        job = s.get(Job, job_id)
        return job.status, job.applied_at


@pytest.fixture
def person_sends(monkeypatch):
    """The person presses Submit on each form; the app sees the confirmation page."""
    monkeypatch.setattr(greenhouse, "watch_submission", lambda page, timeout_s, **kw: ("submitted", {}))


def test_an_application_sent_from_the_list_says_where_it_came_from_and_exactly_what_went(
        db, desk, client, person_sends):
    cfg, _, _ = db
    c, _ = client
    execution.add([1])
    runner.prepare_chosen(cfg)
    made_ready = {kind: s for (kind, state), s in artifacts(1).items()}
    runner.run(cfg, chosen=True, browser_factory=Browser)
    item, form = item_of(1), Browser.forms[0]
    [line] = audit(1)                                                   # one line, linked
    link = line["queue"]
    assert (link["item_id"], link["run_id"], link["attempt"]) == (item.id, runs()[-1], 2)   # made ready on 1
    sent = {d["kind"]: d for d in link["documents"]}
    assert sent["resume"]["sent"] == form.files["input#resume"] == line["resume"]
    assert sent["cover_letter"]["sent"] == form.files["input#cover_letter"] == line["cover_letter"]
    for kind in ("resume", "cover_letter"):
        assert sent[kind]["sha256"] == made_ready[kind] == sha(sent[kind]["sent"])           # exactly that file
        assert sent[kind]["made_ready_in_run"] == runs()[0]                                  # by Prepare these
    assert artifacts(1) == {("resume", "sent"): made_ready["resume"], ("cover_letter", "sent"): made_ready["cover_letter"]}
    # The Applied page says the same; the list's entry links to it; the job and the history agree.
    record = tracker.application(1)["record"]
    assert record["queue"] == {"run_id": runs()[-1], "attempt": 2, "documents": [
        {"kind": "resume", "name": sent["resume"]["name"], "sha256": made_ready["resume"]},
        {"kind": "cover_letter", "name": sent["cover_letter"]["name"], "sha256": made_ready["cover_letter"]}]}
    page = c.get("/applications/1").get_data(as_text=True)
    assert f"From your chosen jobs on the Auto-apply page: run {runs()[-1]}, attempt 2" in page
    assert made_ready["resume"][:12] in page and made_ready["cover_letter"][:12] in page
    shown = next(i for i in c.get("/apply/chosen").get_json()["items"] if i["job_id"] == 1)
    assert shown["state"] == APPLIED and {d["state"] for d in shown["documents"]} == {"sent"}
    status, when = applied_at(1)
    assert status == "Applied" and when is not None
    assert 1 in {a["job_id"] for a in tracker.applications()}
    assert item.outcome.startswith("Applied ✓ (you submitted it")


def test_the_persons_own_word_is_linked_too_and_counts_once(db, desk, monkeypatch):
    cfg, _, _ = db
    monkeypatch.setattr(greenhouse, "watch_submission", lambda page, timeout_s, **kw: ("timeout", {}))
    execution.add([1, 6])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert item_of(1).state == item_of(6).state == PENDING and audit(1) == audit(6) == []
    execution.confirm(item_of(1).id, went=True)                         # on the Auto-apply page
    execution.settled_elsewhere(6, True)                                # on the job's own page
    for job_id in (1, 6):
        [line] = audit(job_id)
        assert line["queue"]["item_id"] == item_of(job_id).id and line["queue"]["attempt"] == 1
        assert {d["kind"]: d["sha256"] for d in line["queue"]["documents"]} == {
            "resume": sha(desk.own(job_id, "resume")), "cover_letter": sha(desk.own(job_id, "letter"))}
        assert {state for (_, state) in artifacts(job_id)} == {"sent"}
    first = applied_at(1)
    with pytest.raises(execution.Conflict):
        execution.confirm(item_of(1).id, went=True)                     # another tab, a double press
    assert execution.settled_elsewhere(1, True) is None                 # nothing waits for that answer now
    assert len(audit(1)) == 1 and applied_at(1) == first


def test_one_record_however_many_say_it_went_and_its_first_day_stands(db, desk, person_sends):
    cfg, _, _ = db
    execution.add([1])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    status, first = applied_at(1)
    # The employer's email, an hour on; the person moves it to Interviewing; the email once more.
    assert runner.record_application(1, {"how": "email"}, at=first.replace(tzinfo=None) + timedelta(hours=1)) is False
    with get_session() as s:
        s.get(Job, 1).status = "Interviewing"
    assert runner.record_application(1, {"how": "email"}) is False
    assert applied_at(1) == ("Interviewing", first) and len(audit(1)) == 1 and "queue" in audit(1)[0]
    execution.sync_with_jobs()
    assert item_of(1).state == APPLIED and item_of(1).outcome.startswith("Applied ✓")       # its own ending kept


def test_one_applied_another_way_is_never_claimed_as_sent_from_the_list(db, desk):
    cfg, _, _ = db
    execution.add([1])
    runner.prepare_chosen(cfg)
    runner.record_application(1, {"company": "Globex", "title": "Platform Engineer", "how": "email"})
    execution.sync_with_jobs()
    assert item_of(1).state == APPLIED and item_of(1).outcome.startswith("Marked Applied another way")
    assert "queue" not in audit(1)[0]                                   # it did not go from the list
    assert {state for (_, state) in artifacts(1)} == {"ready"}          # its documents never went
