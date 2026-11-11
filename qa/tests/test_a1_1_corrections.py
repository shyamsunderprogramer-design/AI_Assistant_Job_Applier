"""A1.1 (8 Nov 2026): the corrections the A1 release review found, each with a test that fails without it.

  * Apply to these crashed on every resume made in the same run (a read-only published file copied with its
    mode, then copied over): the real prepare -> record -> seal -> form path, end to end, only the employer's
    form stood in.
  * A version made ready before a newer one was published could still be sent: only the current version goes.
  * The job page's template was dropped.
  * An edit published twice, and "busy" came back as a server error; Prepare now tailored over an edit.
  * Fencing at publication was a check, then a write: now one statement. A take-over could race a heartbeat.
  * The spending limit was priced low, per job, unreserved, and swallowed by some steps.
  * A redo could return the older, better-scoring version.
  * The repeat key missed the template default, the look, the contact line and the requirements.
  * The person's earlier tailored resumes: kept, reused only from the same inputs, else the person chooses.
  * Test isolation: the person's tailoring folders, lines, titles, scores and job folders.

Made-up jobs in a database file of the test's own, the tailoring run stood in for (no model), files in the
test's own folders; no network, no AI tool, no real application."""

import os
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.apply import execution, runner
from backend.apply.execution import FAILED, QUEUED
from data_engineering.db.models import ApplyArtifact, Job
from data_engineering.db.session import get_session
from ml.resume import cost, llm
from ml.tailoring import coordination as co
from ml.tailoring import pipeline
from qa.tests.test_canonical_preparation import Runs, job, made, versions  # noqa: F401
from qa.tests.test_execution_queue import Context, client, db, item_of  # noqa: F401


# -- the employer's form, stood in for: what it was given --------------------------------------------------

@pytest.fixture
def form(monkeypatch):
    given = []

    def fill_form(cfg, page, j, applicant, head, ready, *, auto, wait_s):
        given.append({"job": j.id, "resume": ready.kept, "sha": co.sha256(ready.kept), "auto": auto})
        runner.record_application(j.id, {"company": j.company, "title": j.title, "how": "you"})
        return runner.Outcome(j.id, j.company, j.title, "submitted", how="you")
    monkeypatch.setattr(runner, "fill_form", fill_form)
    monkeypatch.setattr(runner, "write_letter_if_missing", lambda cfg, j, head="": False)
    monkeypatch.setattr(runner, "cover_letter", lambda cfg, j, evidence: None)
    from backend.apply import prepare
    monkeypatch.setattr(prepare, "open_questions", lambda j, applicant: [])
    return given


# -- 1. the crash: the real path, end to end ------------------------------------------------------------------

def test_apply_to_these_makes_seals_and_sends_a_resume_made_in_the_same_run(made, form):
    execution.add([1])
    runner.run(made.cfg, chosen=True, browser_factory=Context)       # A1: PermissionError at seal
    p = co.current(1)
    assert item_of(1).state == "applied", item_of(1).outcome
    assert [g["sha"] for g in form] == [p.pdf_sha256]                 # exactly the published version went
    assert not os.access(p.pdf, os.W_OK) and co.sha256(p.pdf) == p.pdf_sha256   # still read-only, untouched
    evidence = form[0]["resume"].parent.parent
    assert os.access(form[0]["resume"], os.W_OK) and os.access(evidence / "resume.pdf", os.W_OK)


def test_prepare_these_then_apply_to_these_seals_without_error_too(made, form):
    execution.add([1])
    runner.prepare_chosen(made.cfg)
    runner.run(made.cfg, chosen=True, browser_factory=Context)
    assert item_of(1).state == "applied" and form[0]["sha"] == co.current(1).pdf_sha256


# -- 2. only the current version is sent -----------------------------------------------------------------------

def test_a_version_made_ready_before_a_newer_one_is_never_sent(made, form):
    execution.add([1])
    runner.prepare_chosen(made.cfg)                                    # made ready with version 1
    first = co.current(1)
    folder = pipeline.folder_of(first.run_id)
    os.chmod(folder / "tailored-resume.pdf", 0o644)
    (folder / "tailored-resume.pdf").write_bytes(b"%PDF the person's edit " * 60)
    pipeline.publish_edit(1, folder, note="edited by you")              # version 2
    second = co.current(1)
    assert second.version == 2
    runner.run(made.cfg, chosen=True, browser_factory=Context)
    assert [g["sha"] for g in form] == [second.pdf_sha256]             # never version 1
    steps = [e["message"] for e in execution.events_after(0, item_id=item_of(1).id)]
    assert any("a newer version of its resume (version 2) was published" in m for m in steps)


def test_the_seal_itself_refuses_a_superseded_version(made):
    execution.add([1])
    runner.prepare_chosen(made.cfg)
    pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now", again=True)    # a newer version
    evidence = made.tmp / "attempt"
    why = execution.seal(item_of(1).id, evidence)
    assert isinstance(why, str) and "newer version of its resume" in why
    with get_session() as s:
        assert s.query(ApplyArtifact).filter(ApplyArtifact.item_id == item_of(1).id,
                                             ApplyArtifact.state == "ready").count() == 0


# -- 3. the job page's template -----------------------------------------------------------------------------

def test_the_job_pages_template_reaches_the_tailoring_and_its_key(made, client, monkeypatch):
    seen = []
    real = made.runs.__call__

    def run(folder, resume_file, jd_text="", jd_file=None, cfg=None, progress=lambda s, n="": None, company=None, **kw):
        seen.append(kw.get("template"))
        return real(folder, resume_file, jd_text, jd_file, cfg, progress, company, **kw)
    monkeypatch.setattr(pipeline, "run", run)
    monkeypatch.setattr("backend.api.app.cfg", lambda: made.cfg)
    c, _ = client
    assert c.post("/job/1/tailor-full", json={"template": "modern"}).status_code == 200
    for _ in range(200):
        if co.current(1) is not None:
            break
        time.sleep(0.05)
    assert seen == ["modern"]
    assert co.current(1).repeat_key == pipeline.repeat_key(made.cfg, job(1), made.resume, template="modern")
    assert co.current(1).repeat_key != pipeline.repeat_key(made.cfg, job(1), made.resume)


# -- 4. edits: published once, kept, "busy" said plainly -----------------------------------------------------------

def _edit_setup(made):
    first = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    folder = pipeline.folder_of(first.run_id)
    from ml.tailoring import documents
    return first, folder, documents


def test_an_edit_publishes_once_and_prepare_now_keeps_it(made, client, monkeypatch):
    first, folder, documents = _edit_setup(made)
    draft = SimpleNamespace(result=SimpleNamespace(overrides={}))
    monkeypatch.setattr(pipeline, "load_draft", lambda f: (draft, {}))
    monkeypatch.setattr(pipeline, "_draft_json", lambda d: {})

    def produce(*a, **k):
        os.chmod(folder / "tailored-resume.pdf", 0o644)
        (folder / "tailored-resume.pdf").write_bytes(b"%PDF edited " * 80)
        return SimpleNamespace(docx=folder / "tailored-resume.docx", pdf=folder / "tailored-resume.pdf", pages={},
                               checks=[], ok=True)
    monkeypatch.setattr(documents, "produce", produce)
    c, _ = client
    r = c.post(f"/tailor/{folder.name}/edit", json={"edits": {"a": "b"}})
    assert r.status_code == 200
    assert [v[:2] for v in versions()] == [(1, "published"), (2, "published")]       # one new version, not two
    again = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    assert again.reused and made.runs.calls == 1 and again.publication.version == 2   # the edit is kept


def test_an_edit_while_the_job_is_busy_says_so_plainly(made, client, monkeypatch):
    first, folder, documents = _edit_setup(made)
    monkeypatch.setattr(pipeline, "load_draft", lambda f: (SimpleNamespace(result=SimpleNamespace(overrides={})), {}))
    monkeypatch.setattr(pipeline, "_draft_json", lambda d: {})
    monkeypatch.setattr(documents, "produce", lambda *a, **k: SimpleNamespace(
        docx=folder / "tailored-resume.docx", pdf=folder / "tailored-resume.pdf", pages={}, checks=[], ok=True))
    held = co.acquire(1, "Prepare these")
    c, _ = client
    r = c.post(f"/tailor/{folder.name}/edit", json={"edits": {"a": "b"}})
    assert r.status_code == 409 and r.get_json()["busy"] is True
    co.release(held)


# -- 5. fencing: one statement; a heartbeat stops a take-over --------------------------------------------------------

def test_a_take_over_between_the_copy_and_the_flip_publishes_nothing(made):
    lease = co.acquire(1, "slow")
    docx, pdf = made.tmp / "a.docx", made.tmp / "a.pdf"
    docx.write_bytes(b"D" * 300)
    pdf.write_bytes(b"P" * 300)

    def taken_over(stage):
        if stage == "before_visible":
            co.acquire(1, "Prepare now", now=co._now() + timedelta(seconds=co.LEASE_TTL_S + 5))
    with pytest.raises(co.LeaseLost):
        co.publish(lease, docx=docx, pdf=pdf, source="tailored", repeat_key="k", run_id="r", crash=taken_over)
    assert co.current(1) is None and versions() == [(1, "failed", "tailored")]


def test_a_heartbeat_between_reading_and_taking_over_stops_the_take_over(made, monkeypatch):
    lease = co.acquire(1, "working")
    from sqlalchemy import text
    from data_engineering.db import session as session_mod
    old = co._now() - timedelta(seconds=co.LEASE_TTL_S + 60)
    with session_mod._engine.begin() as c:
        c.execute(text("UPDATE preparation_leases SET heartbeat_at = :t"), {"t": old})
    real_row, state = co._row, {"first": True}

    def row(connection, job_id):
        found = real_row(connection, job_id)
        if state.pop("first", False):                 # it looked expired; the owner beats just now
            co.heartbeat(lease)
        return found
    monkeypatch.setattr(co, "_row", row)
    with pytest.raises(co.LeaseBusy):
        co.acquire(1, "Prepare now")
    co.heartbeat(lease)                                # the owner still holds it


# -- 6. the spending limit ----------------------------------------------------------------------------------------

@pytest.fixture
def billed(monkeypatch, made):
    monkeypatch.setitem(llm.PROVIDERS, "fakebill", {"label": "A billing writer", "free": False})
    monkeypatch.setattr(llm, "provider_name", lambda cfg=None: "fakebill")
    monkeypatch.setattr(llm, "model_name", lambda cfg=None, **k: "fake-model")
    monkeypatch.setattr(runner, "writer", lambda cfg: ("fakebill", "A billing writer", False))
    monkeypatch.setattr(cost, "pricing_for", lambda model, cfg=None: {"input": 0.0, "output": 1.0})   # $1 / M out
    sent = []

    def dispatch(provider, system, user, cfg, max_tokens=16000, **kw):
        sent.append(max_tokens)
        time.sleep(0.05)
        # It writes as much as it was allowed: the real cost of each call is its full price.
        return llm.Completion("{}", "fake-model", provider, SimpleNamespace(input_tokens=0, output_tokens=max_tokens))
    monkeypatch.setattr(llm, "_dispatch", dispatch)
    return sent


def test_a_call_is_priced_at_its_full_allowed_length(billed):
    with cost.budget(cost.Ledger(cap_usd=0.012)):     # 12k tokens: enough for 8k (A1's price), not for 16k
        with pytest.raises(cost.SpendCapReached):
            llm.complete("s", "u", None, max_tokens=16000)
    assert billed == []


def test_two_calls_at_once_cannot_both_slip_under_the_limit(billed):
    errors, gate = [], threading.Barrier(2)

    def call():
        gate.wait()
        try:
            llm.complete("s", "u", None, max_tokens=10000)
        except cost.SpendCapReached:
            errors.append(1)
    with cost.budget(cost.Ledger(cap_usd=0.015)):     # room for one 10k-token call, not two
        threads = [threading.Thread(target=call) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert len(billed) == 1 and len(errors) == 1


def test_one_limit_covers_the_whole_run_not_each_job(made, billed, monkeypatch):
    made.cfg["resume.max_spend_per_run_usd"] = 0.02           # room for one job's call, not two
    made.runs.during = {pipeline.STAGES[2]: lambda: llm.complete("s", "u", made.cfg, max_tokens=12000)}
    execution.add([1, 6])
    runner.prepare_chosen(made.cfg, paid_ok=True)
    states = sorted((item_of(1).state, item_of(6).state))
    assert states == [FAILED, QUEUED]
    assert billed.count(12000) == 1                           # the second job's tailoring call never went
    assert sum(billed) / 1_000_000 <= 0.02                    # everything the run spent (its letter too) fits
    failed = item_of(1) if item_of(1).state == FAILED else item_of(6)
    assert "spending limit" in failed.outcome


def test_a_limit_hit_while_reading_the_posting_stops_the_run(billed):
    from ml.tailoring import requirements
    with cost.budget(cost.Ledger(cap_usd=0.000001)):
        with pytest.raises(cost.SpendCapReached):             # A1: swallowed, "found by rules instead"
            requirements.extract("Platform Engineer\n\nRequired: Terraform. " * 20, None, use_model=True)


# -- 7. redo, and the repeat key ----------------------------------------------------------------------------------

def test_a_redo_publishes_the_new_version_even_if_the_old_scored_higher(made, monkeypatch):
    pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    monkeypatch.setattr(pipeline, "_better", lambda job_id, folder, score: False)
    pipeline.mark_redo([1], "made from a master since repaired")
    done = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    assert not done.reused and done.publication.version == 2 and pipeline.needs_redo(1) is None


@pytest.mark.parametrize("change", ["template_default", "look", "contact", "requirements"])
def test_each_input_the_document_depends_on_changes_the_key(made, monkeypatch, change):
    base = pipeline.repeat_key(made.cfg, job(1), made.resume)
    if change == "template_default":
        made.cfg["resume.template"] = "compact"
        after = pipeline.repeat_key(made.cfg, job(1), made.resume)
    elif change == "look":
        after = pipeline.repeat_key(made.cfg, job(1), made.resume, look={"font": "Calibri"})
    elif change == "contact":
        monkeypatch.setattr(pipeline, "_contact_fingerprint", lambda: "a new phone number")
        after = pipeline.repeat_key(made.cfg, job(1), made.resume)
    else:
        with get_session() as s:
            s.get(Job, 1).requirements = "Kubernetes"
        after = pipeline.repeat_key(made.cfg, job(1), made.resume)
    assert after != base


# -- 8. the person's earlier tailored resumes: kept; reused from the same inputs only; else they choose ---------------

def _earlier(made, posting=None):
    folder = pipeline.new_run()
    pipeline.save_saved_resume(folder, made.resume)
    j = job(1)
    (folder / "inputs" / "job.txt").write_text(posting if posting is not None else f"{j.title}\n\n{j.description or ''}")
    (folder / "tailored-resume.docx").write_bytes(b"DOCX earlier " * 300)
    (folder / "tailored-resume.pdf").write_bytes(b"%PDF earlier " * 300)
    pipeline._write_json(folder / "result.json", {"draft_note": "", "checks_ok": True})
    pipeline._write_json(pipeline.JOBS / "1.json", {"run": folder.name, "best": folder.name, "best_score": 88.0,
                                                    "measure": "ats"})
    return folder


def test_an_earlier_resume_made_from_other_inputs_is_kept_and_the_person_chooses(made, client, monkeypatch):
    folder = _earlier(made, posting="an earlier version of the posting")
    before = {f.name: co.sha256(f) for f in folder.iterdir() if f.is_file()}
    execution.add([1])
    runner.prepare_chosen(made.cfg)
    item = item_of(1)
    assert made.runs.calls == 0                                   # no tailoring run by itself
    assert item.state == FAILED and "earlier tailored resume" in item.outcome and "Tailor it again" in item.outcome
    view = next(i for i in execution.view(made.cfg)["items"] if i["id"] == item.id)
    assert view["can"]["again"] and view["can"]["earlier"] and view["can"]["own"]
    assert {f.name: co.sha256(f) for f in folder.iterdir() if f.is_file()} == before          # untouched
    c, _ = client
    monkeypatch.setattr("backend.api.chosen_pages.cfg", lambda: made.cfg)
    r = c.post("/apply/chosen/use-earlier", json={"item": item.id})
    assert r.status_code == 200 and item_of(1).state == QUEUED
    p = co.current(1)
    assert p.run_id == folder.name and "chosen by you" in p.note and co.sha256(p.pdf) == before["tailored-resume.pdf"]


def test_tailor_it_again_makes_a_new_one_and_keeps_the_earlier(made, client):
    folder = _earlier(made, posting="an earlier version of the posting")
    execution.add([1])
    runner.prepare_chosen(made.cfg)
    c, _ = client
    assert c.post("/apply/chosen/tailor-again", json={"item": item_of(1).id}).status_code == 200
    runner.prepare_chosen(made.cfg)
    assert made.runs.calls == 1 and co.current(1).run_id != folder.name
    assert (folder / "tailored-resume.pdf").read_bytes().startswith(b"%PDF earlier")   # the earlier one is kept


def test_an_earlier_resume_from_the_same_inputs_is_reused_without_a_model(made):
    folder = _earlier(made)
    done = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare these")
    assert done.reused and made.runs.calls == 0 and done.publication.run_id == folder.name


def test_the_choices_are_offered_only_for_that_case(made, client):
    execution.add([1])
    c, _ = client
    for route in ("/apply/chosen/tailor-again", "/apply/chosen/use-earlier"):
        assert c.post(route, json={"item": item_of(1).id}).status_code == 409   # queued, not that failure


# -- 9. test isolation -------------------------------------------------------------------------------------------

def test_the_persons_lines_titles_scores_and_job_folders_are_never_the_tests(tmp_path):
    from backend.config.loader import PROJECT_ROOT
    from ml.resume import additions, packet, titles
    assert additions.ADDITIONS_PATH != PROJECT_ROOT / "ml" / "resume" / "additions.yaml"
    assert titles.TITLES_PATH != PROJECT_ROOT / "ml" / "resume" / "titles.yaml"
    assert pipeline.SCORES != PROJECT_ROOT / "data" / "tailoring" / "scores"
    assert packet._places_file(None) != PROJECT_ROOT / "ml" / "resume" / "output" / ".folder-places.json"
    assert co.PUBLISHED != PROJECT_ROOT / "data" / "tailoring" / "published"
    assert pipeline.OWN != PROJECT_ROOT / "data" / "tailoring" / "own"


def test_the_tripwire_notices_a_change_inside_a_tailoring_folder(tmp_path):
    from qa.tests import conftest
    assert {"data/tailoring/jobs", "data/tailoring/runs", "data/tailoring/published"} <= set(conftest.OWN_FOLDERS)
    folder = tmp_path / "jobs"
    folder.mkdir()
    before = conftest._listing(folder, deep=False)
    (folder / "7.json").write_text("{}")
    assert conftest._listing(folder, deep=False) != before


# -- the second review's findings (8 Nov 2026) ------------------------------------------------------------------

def test_a_damaged_newest_version_lets_no_older_one_through(made):
    execution.add([1])
    runner.prepare_chosen(made.cfg)                                    # made ready with version 1
    second = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now", again=True).publication
    os.chmod(second.pdf, 0o644)
    second.pdf.write_bytes(b"%PDF damaged")                            # the newest version's file changes
    artifact = execution.documents(item_of(1).id)[0]
    assert "newer version" in execution.superseded(1, artifact)        # A1.1 first cut: None, and v1 went


def test_a_job_whose_versions_were_all_withdrawn_sends_none_of_them(made):
    execution.add([1])
    runner.prepare_chosen(made.cfg)
    co.withdraw(1, 1, "rolled back by the person")
    artifact = execution.documents(item_of(1).id)[0]
    assert "withdrawn" in execution.superseded(1, artifact)


def test_the_job_page_and_prepare_these_use_one_template_so_one_key(made, client, monkeypatch):
    monkeypatch.setattr("backend.api.app.cfg", lambda: made.cfg)
    c, _ = client
    assert c.post("/job/1/tailor-full", json={}).status_code == 200    # no choice: the job's own template
    for _ in range(200):
        if co.current(1) is not None:
            break
        time.sleep(0.05)
    again = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare these")
    assert again.reused and made.runs.calls == 1                       # not tailored over the page's version


def test_a_billed_call_that_fails_still_counts_its_price(billed, monkeypatch):
    def fails(provider, system, user, cfg, max_tokens=16000, **kw):
        raise RuntimeError("timed out after the provider started billing")
    monkeypatch.setattr(llm, "_dispatch", fails)
    ledger = cost.Ledger(cap_usd=0.025)
    with cost.budget(ledger):
        for _ in range(2):
            with pytest.raises(RuntimeError):
                llm.complete("s", "u", None, max_tokens=10000)
        with pytest.raises(cost.SpendCapReached):                      # two failures used the room
            llm.complete("s", "u", None, max_tokens=10000)
    assert round(ledger.spent, 3) == 0.02


def test_a_held_price_is_let_go_by_the_budget_that_held_it(billed):
    first = cost.Ledger(cap_usd=1.0)
    with cost.budget(first):
        held = cost.check_billed_call("fake-model", "u", 10000)
        later = cost.Ledger(cap_usd=1.0)
        with cost.budget(later):                                       # opened while the call is under way
            cost.settle_billed_call("fake-model", SimpleNamespace(input_tokens=0, output_tokens=10000), held)
        assert cost._RESERVED.get(id(first), 0.0) == 0.0 and not later.calls and len(first.calls) == 1
