"""Step 3 of the Morning shortlist plan (4 Nov 2026): a pick's documents are made only when the person presses
Prepare -- never by the morning, never by itself -- by the same run as Prepare these, for exactly the entries
pressed for; with plain statuses, their approval as before, and interruption, retry, a second press and
recovery handled. And: no job set aside for good is ever picked again, and an unknown sponsorship is labelled
"Sponsorship unknown" everywhere, its decision unchanged. Made-up jobs, a temporary database file, a stand-in
for the writer; nothing is tailored, sent or billed."""

import json
import os
from datetime import timedelta

import pytest

from backend.apply import approval, execution, runner, shortlist
from backend.core import eligibility
from data_engineering.db.models import ApplyArtifact, ApplyEvent, ApplyItem, ApplyRun, Job, ShortlistPick
from data_engineering.db.session import get_session
from qa.tests.test_morning_picks_page import OFF, ON, client, jobs_db, now, picks_now, post  # noqa: F401
from qa.tests.test_morning_shortlist import Cfg


class Writer:
    """The writer, stood in for: each job's resume "made" in a moment, free; which jobs it made, which fail."""
    def __init__(self):
        self.made, self.fails, self.version = [], set(), 1


@pytest.fixture
def writer(monkeypatch, tmp_path):
    w = Writer()

    def prepare_documents(cfg, job, applicant, head, *, tailor, **_):
        w.made.append(job.id)
        if job.id in w.fails:
            return "the writer gave up"
        folder = tmp_path / f"made-{job.id}"
        folder.mkdir(exist_ok=True)
        (folder / "resume.pdf").write_bytes(f"%PDF-1.4 resume for {job.id} v{w.version}".encode())
        return runner.Prepared(folder / "resume.pdf", None, "tailored", folder)
    monkeypatch.setattr(runner, "prepare_documents", prepare_documents)
    monkeypatch.setattr(runner, "writer", lambda cfg: ("ollama", "This Mac's model", True))
    monkeypatch.setattr(runner, "say", lambda line: None)
    from backend.apply import prepare
    monkeypatch.setattr(prepare, "open_questions", lambda job, applicant: [])
    return w


def started_items(started):
    (task, args), = started
    assert task == "prepare-chosen" and args[:2] == ["prepare-apply", "--chosen"]
    return [int(i) for i in args[args.index("--items") + 1].split(",")]


@pytest.fixture
def tasks(monkeypatch):
    from backend.api import app as webapp
    started = []
    pid = {"value": None}
    monkeypatch.setattr("backend.api.shortlist_pages.start_task",
                        lambda task, args: started.append((task, args)) or webapp.jsonify(ok=True))
    monkeypatch.setattr("backend.api.shortlist_pages.task_pid", lambda task: pid["value"])
    return started, pid


def item_for(job_id):
    with get_session() as s:
        return s.query(ApplyItem).filter_by(job_id=job_id).order_by(ApplyItem.id.desc()).first()


def stage_of(c, job_id):
    return next(p["stage"] for p in picks_now(c)["picks"] if p["job_id"] == job_id)


# -- nothing is made before the person presses ------------------------------------------------------------

def test_the_morning_never_makes_a_document_or_calls_a_writer(client, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("the morning's picks must not write, tailor or bill anything")
    from ml.resume import llm
    for target, name in ((runner, "prepare_documents"), (llm, "complete"), (execution, "prepare_these"),
                         (execution, "start_run")):
        monkeypatch.setattr(target, name, never)
    from backend.core import daily
    import data_engineering.excel.tracker as tracker
    import ml.resume.pipeline as pipeline
    monkeypatch.setattr(pipeline, "score_jobs", lambda cfg: [])
    monkeypatch.setattr(tracker, "export_jobs", lambda cfg, only_new=True: (0, 0, 0))
    monkeypatch.setattr(daily, "stale_companies", lambda days: [])
    report = daily.DailyReport()
    daily._rank_and_gather(report, ON, now() - timedelta(hours=24))
    assert report.ok and [s.name for s in report.stages][-1] == "shortlist"
    c, _ = client
    d = picks_now(c)
    assert len(d["picks"]) == 5
    assert {p["stage"]["key"] for p in d["picks"]} == {"selected"}             # waiting for Prepare
    assert all(p["can"]["prepare"] for p in d["picks"])
    with get_session() as s:
        assert s.query(ApplyArtifact).count() == 0 and s.query(ApplyRun).count() == 0


def test_prepare_on_one_pick_starts_the_same_run_for_that_entry_only(client, tasks):
    c, _ = client
    started, _ = tasks
    shortlist.pick(ON, now=now())
    r = c.post("/apply/picks/prepare", json={"job_id": 2})
    assert r.status_code == 200 and started_items(started) == [item_for(2).id]


def test_prepare_todays_picks_is_for_the_waiting_ones_without_documents(client, tasks, writer):
    c, _ = client
    started, _ = tasks
    shortlist.pick(ON, now=now())
    execution.add([13])                                                           # yours: never in a picks' press
    post(c, "remove", 12)                                                         # off the list: not prepared
    assert c.post("/apply/picks/prepare", json={}).status_code == 200
    ids = started_items(started)
    assert ids == [item_for(j).id for j in (1, 2, 4, 11)]
    runner.prepare_chosen(ON, only=ids)                                           # what that task runs
    assert sorted(writer.made) == [1, 2, 4, 11] and item_for(13).state == "queued"
    with get_session() as s:
        assert s.query(ApplyArtifact).filter_by(item_id=item_for(13).id).count() == 0
    assert c.post("/apply/picks/prepare", json={}).status_code == 409             # nothing left to make


@pytest.mark.parametrize("body", [{"job_id": "2"}, {"job_id": True}, {"job_id": 0}])
def test_a_prepare_without_a_proper_job_is_refused(client, tasks, body):
    c, _ = client
    assert c.post("/apply/picks/prepare", json=body).status_code == 400
    assert tasks[0] == []


def test_a_second_press_while_a_run_goes_is_refused_and_the_database_allows_one_run(client, tasks):
    c, _ = client
    started, pid = tasks
    shortlist.pick(ON, now=now())
    pid["value"] = 4242                                                           # the first press's task, going
    r = c.post("/apply/picks/prepare", json={})
    assert r.status_code == 409 and "A run is going already" in r.get_json()["error"]
    pid["value"] = None
    run = execution.start_run(os.getpid())                                        # or a run in the database
    assert c.post("/apply/picks/prepare", json={"job_id": 1}).status_code == 409
    with pytest.raises(execution.QueueError):
        execution.start_run(os.getpid())                                          # two at once: never
    execution.finish_run(run.id, "done", "")
    assert started == []


def test_a_writer_that_bills_asks_first_and_your_details_come_first(client, tasks, monkeypatch):
    c, _ = client
    started, _ = tasks
    shortlist.pick(ON, now=now())
    monkeypatch.setattr(runner, "writer", lambda cfg: ("openai", "OpenAI", False))
    r = c.post("/apply/picks/prepare", json={"job_id": 1})
    assert r.status_code == 409 and r.get_json()["paid_writer"] == "OpenAI" and started == []
    assert c.post("/apply/picks/prepare", json={"job_id": 1, "paid": True}).status_code == 200
    assert "--paid-ok" in started[0][1]
    monkeypatch.setattr(runner, "check_ready", lambda applicant: ["your phone number"])
    r = c.post("/apply/picks/prepare", json={"job_id": 2})
    assert r.status_code == 409 and "your phone number" in r.get_json()["error"]


def test_a_billing_writer_is_never_used_by_the_run_without_the_yes(client, writer, monkeypatch):
    shortlist.pick(ON, now=now())
    monkeypatch.setattr(runner, "writer", lambda cfg: ("openai", "OpenAI", False))
    assert runner.prepare_chosen(ON, only=[item_for(1).id]) == 0 and writer.made == []


# -- statuses, approval ------------------------------------------------------------------------------------

def test_each_pick_says_where_it_stands_from_selected_to_ready_for_review(client, writer):
    c, _ = client
    shortlist.pick(ON, now=now())
    assert stage_of(c, 1)["label"] == "Selected" and "Nothing is made before you do" in stage_of(c, 1)["hint"]
    runner.prepare_chosen(ON, only=[item_for(1).id])
    st = stage_of(c, 1)
    assert st["key"] == "approval" and st["label"] == "Needs document approval"
    pick = next(p for p in picks_now(c)["picks"] if p["job_id"] == 1)
    assert pick["can"]["prepare"] is False                                        # made: not made again
    doc = pick["entry"]["documents"][0]
    assert approval.decide(item_for(1).id, doc["id"], "approved")
    assert stage_of(c, 1)["label"] == "Ready for review"
    writer.version = 2                                                            # made again: another document
    with get_session() as s:
        s.query(ApplyArtifact).filter_by(item_id=item_for(1).id).update({"state": "replaced"})
    runner.prepare_chosen(ON, only=[item_for(1).id])
    assert stage_of(c, 1)["label"] == "Needs document approval"                 # its approval does not carry over
    doc = next(p for p in picks_now(c)["picks"] if p["job_id"] == 1)["entry"]["documents"][0]
    approval.decide(item_for(1).id, doc["id"], "rejected")
    assert "never sent" in stage_of(c, 1)["hint"]


def test_the_stages_cover_every_state_of_the_list():
    S = shortlist.stage
    assert S(None)["key"] == "gone"
    q = {"state": "queued", "documents": []}
    assert S({**q, "note": "Waits for your answer: US citizens only"})["label"] == "Needs your confirmation"
    assert S({**q, "note": "Waits for your answers: 2 required questions"})["label"] == "Needs answers"
    assert S({"state": "preparing", "current": "Tailoring"})["label"] == "Preparing documents"
    assert S({"state": "waiting_user", "action": "Press Submit"})["label"] == "Ready for manual submission"
    assert S({"state": "pending_confirmation"})["label"] == "Did it go?"
    assert S({"state": "applied"})["label"] == "Applied"
    assert S({"state": "failed", "outcome": "Not made ready: the writer gave up."})["label"] == "Failed / blocked"
    assert S({"state": "cancelled"}, removed=True)["label"] == "Removed"
    assert S({"state": "cancelled"})["label"] == "Taken off your list"


def test_a_pick_whose_eligibility_needs_your_answer_waits_and_is_not_made(client, writer):
    c, _ = client
    shortlist.pick(Cfg({**ON, "shortlist.allow_needs_confirmation": True, "shortlist.per_day": 20}), now=now())
    runner.prepare_chosen(ON, only=[item_for(7).id])
    assert writer.made == [] and stage_of(c, 7)["label"] == "Needs your confirmation"


# -- interruption, retry, recovery, a press made before -----------------------------------------------------------

def test_a_failed_preparation_says_why_and_retry_then_prepare_makes_it(client, writer):
    c, _ = client
    shortlist.pick(ON, now=now())
    writer.fails = {2}
    runner.prepare_chosen(ON, only=[item_for(2).id])
    st = stage_of(c, 2)
    assert st["label"] == "Failed / blocked" and "the writer gave up" in st["hint"]
    writer.fails = set()
    execution.retry(item_for(2).id)
    assert stage_of(c, 2)["label"] == "Selected"
    runner.prepare_chosen(ON, only=[item_for(2).id])
    assert stage_of(c, 2)["label"] == "Needs document approval"


def test_a_run_stopped_halfway_is_recovered_with_nothing_lost_or_sent(client, writer):
    c, _ = client
    shortlist.pick(ON, now=now())
    run = execution.start_run(999_999)                                            # a process that is gone
    item = item_for(4).id
    execution.move(item, execution.VALIDATING, run_id=run.id, expect=execution.QUEUED)
    execution.move(item, execution.PREPARING, run_id=run.id, expect=execution.VALIDATING)
    assert stage_of(c, 4)["label"] == "Preparing documents"
    assert execution.recover()["requeued"] == 1
    assert stage_of(c, 4)["label"] == "Selected" and item_for(4).state == "queued"
    runner.prepare_chosen(ON, only=[item])
    assert stage_of(c, 4)["label"] == "Needs document approval"
    assert not runner.AUDIT_LOG.exists()


def test_a_press_made_before_the_morning_never_prepares_the_mornings_picks(client, writer, monkeypatch):
    execution.add([13])                                                           # yours, pressed for at night
    added = []
    real = runner.prepare_documents

    def meanwhile(cfg, job, applicant, head, *, tailor, **_):
        if not added:
            added.append(shortlist.pick(ON, now=now()))                           # 6 AM comes during the run
        return real(cfg, job, applicant, head, tailor=tailor)
    monkeypatch.setattr(runner, "prepare_documents", meanwhile)
    runner.prepare_chosen(ON)                                                     # Prepare these: the list as it was
    assert len(added[0]["picked"]) == 5 and writer.made == [13]
    with get_session() as s:
        made = {a.item_id for a in s.query(ApplyArtifact)}
    assert made == {item_for(13).id}


def test_preparing_never_holds_up_the_morning(client, monkeypatch):
    from backend.core import daily
    import data_engineering.excel.tracker as tracker
    import ml.resume.pipeline as pipeline
    monkeypatch.setattr(pipeline, "score_jobs", lambda cfg: [])
    exported = []
    monkeypatch.setattr(tracker, "export_jobs", lambda cfg, only_new=True: exported.append(1) or (0, 0, 0))
    monkeypatch.setattr(daily, "stale_companies", lambda days: [])
    run = execution.start_run(os.getpid())                                        # a Prepare going at 6 AM
    report = daily.DailyReport()
    daily._rank_and_gather(report, ON, now() - timedelta(hours=24))
    assert report.ok and exported and [s.name for s in report.stages] == ["score", "export", "digest", "shortlist"]
    assert "5 picked" in report.stages[-1].detail
    with get_session() as s:
        assert s.get(ApplyRun, run.id).state == "running"                         # untouched by the morning
    execution.finish_run(run.id, "done", "")


# -- set aside for good is never picked again (the once-a-day rule) ---------------------------------------------------

def test_nothing_set_aside_for_good_is_ever_picked_again(client):
    c, _ = client
    many = Cfg({**ON, "shortlist.per_day": 20})
    first = shortlist.pick(many, now=now())
    assert sorted(p["job_id"] for p in first["picked"]) == [1, 2, 4, 11, 12, 13]
    post(c, "remove", 1)                                                          # removed
    execution.cancel(item_for(2).id)                                              # cancelled on Your chosen jobs
    with get_session() as s:
        s.get(ApplyItem, item_for(4).id).state = "failed"                         # failed
        s.get(ApplyItem, item_for(11).id).state = "skipped"                       # skipped
        s.get(ApplyItem, item_for(12).id).state = "applied"                       # applied to through the list
        s.get(Job, 12).status = "Applied"
        s.get(ApplyItem, item_for(13).id).state = "cancelled"
        s.get(Job, 13).status = "Rejected"                                        # and turned down
    # Not today can no longer bring back a job that is off the list.
    for job in (1, 2, 4, 11, 12):
        r = post(c, "defer", job)
        assert r.status_code == 409, job
    for day in range(1, 8):
        report = shortlist.pick(many, now=now() + timedelta(days=day))
        assert not {1, 2, 4, 11, 12, 13} & {p["job_id"] for p in report["picked"]}, day
    with get_session() as s:
        assert s.query(ShortlistPick).filter(ShortlistPick.job_id.in_([1, 2, 4, 11, 12, 13])).count() == 6


def test_applied_to_or_marked_by_you_stays_out_even_if_restored(client):
    c, _ = client
    many = Cfg({**ON, "shortlist.per_day": 20})
    shortlist.pick(many, now=now())
    post(c, "remove", 1)
    post(c, "remove", 2)
    post(c, "remove", 4)
    with get_session() as s:
        s.get(Job, 1).applied_at = now().replace(tzinfo=None)                     # applied to since, elsewhere
        s.get(Job, 2).status = "Skipped"                                           # marked by you
        s.get(Job, 4).status = "Interviewing"                                      # and one well on its way
    runner.AUDIT_LOG.write_text(json.dumps({"job_id": 11, "company": "Acme", "title": "Platform Engineer"}) + "\n")
    execution.cancel(item_for(11).id)
    post(c, "remove", 11)
    for job in (1, 2, 4, 11):
        assert post(c, "restore", job).status_code == 200
    report = shortlist.pick(many, now=now() + timedelta(days=1))
    assert not {1, 2, 4, 11} & {p["job_id"] for p in report["picked"]}
    why = {r["job_id"]: r["reason"] for r in report["left_out"]}
    assert why[1] == why[11] == "you applied to it already"
    assert 2 not in why and 4 not in why                                          # marked: never even looked at


# -- "Sponsorship unknown", the decision unchanged ---------------------------------------------------------------

def test_an_unknown_sponsorship_is_labelled_so_and_its_decision_is_unchanged():
    from types import SimpleNamespace
    job = SimpleNamespace(id=1, title="Engineer", company="Acme", description="We build things. " * 20,
                          requirements="", clearance=None, clearance_active=None, polygraph=None,
                          citizenship=None, sponsorship=None, eligibility_manual=None)
    d = eligibility.decide(job, {"requires_sponsorship": True, "authorised_to_work": True})
    assert d.decision == eligibility.WITH_WARNINGS and d.label == "Eligible, with warnings"
    assert d.warnings[0] == eligibility.SPONSORSHIP_UNKNOWN and d.warnings[0].startswith("Sponsorship unknown")
    old = {**d.as_dict(), "reasons": ["doesn't say whether it sponsors a visa, and you need one"],
           "warnings": ["doesn't say whether it sponsors a visa, and you need one"]}
    shown = eligibility.relabel(old)
    assert shown["reasons"] == shown["warnings"] == [eligibility.SPONSORSHIP_UNKNOWN]
    assert (shown["decision"], shown["rule_version"]) == (old["decision"], old["rule_version"])


def test_a_decision_kept_before_is_shown_with_the_new_label_and_its_history_kept(client):
    c, _ = client
    shortlist.pick(ON, now=now())
    item = item_for(1).id
    with get_session() as s:
        row = s.get(ApplyItem, item)
        kept = json.loads(row.decision_detail)
        kept["reasons"] = kept["warnings"] = ["doesn't say whether it sponsors a visa, and you need one"]
        row.decision_detail = json.dumps(kept)                                    # as one decided on 27 Oct
        before = [e.message for e in s.query(ApplyEvent).filter_by(item_id=item)]
    shown = execution.item_views([item], False)[0]["decision"]
    assert shown["reasons"] == [eligibility.SPONSORSHIP_UNKNOWN] and shown["decision"] == "ELIGIBLE_WITH_WARNINGS"
    with get_session() as s:
        assert [e.message for e in s.query(ApplyEvent).filter_by(item_id=item)] == before
        assert json.loads(s.get(ApplyItem, item).decision_detail)["reasons"] == kept["reasons"]   # stored as it was
