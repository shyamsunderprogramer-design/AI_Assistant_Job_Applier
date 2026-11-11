"""Track A1: one canonical resume preparation (ml/tailoring/pipeline.prepare_for_job) for every normal
entry point. It is published only when really tailored; an unfinished or crashed run is never taken for
done; nothing takes a failed resume's place except the person's own, on their press; a cancel stops it;
a worker that lost the job's claim publishes nothing; the same inputs are not made twice; a changed file
waits for approval again; a writer that bills is held to a hard limit. Made-up jobs in a database file of
the test's own, a stand-in for the tailoring run (no model), files in the test's own folders."""

import json
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.apply import approval, execution, prepare, runner
from backend.apply.execution import CANCELLED, FAILED, PREPARING, QUEUED
from data_engineering.db.models import ApplyArtifact, DocumentApproval, Job
from data_engineering.db.session import get_session
from ml.resume import cost, llm
from ml.tailoring import coordination as co
from ml.tailoring import pipeline
from qa.tests.test_execution_queue import Cfg, client, db, item_of  # noqa: F401

HERE = str(Path(__file__).resolve().parents[2])


# -- the stand-in tailoring run ------------------------------------------------------------------------------

class Runs:
    """pipeline.run, stood in for: each call makes a DOCX and a PDF for its posting, says each stage as the
    real run does, and can be told what to do instead (fail, write an untailored draft, ...)."""

    def __init__(self):
        self.calls, self.state, self.raise_at, self.during = 0, "tailored", None, {}

    def __call__(self, folder, resume_file, jd_text="", jd_file=None, cfg=None, progress=lambda s, n="": None,
                 company=None, **kw):
        self.calls += 1
        for stage in pipeline.STAGES:
            progress(stage)
            if stage in self.during:
                self.during[stage]()
            if stage == self.raise_at:
                raise RuntimeError("the writer stopped answering")
        tag = f"{len(jd_text)}-{self.calls}"
        (folder / "tailored-resume.docx").write_bytes(f"DOCX for {company} {tag} ".encode() * 300)
        (folder / "tailored-resume.pdf").write_bytes(f"%PDF for {company} {tag} ".encode() * 300)
        result = {"score": 80.0, "before_after": {"ats": [60, 80 + self.calls]}, "checks": [], "checks_ok": True,
                  "draft_state": self.state,
                  "draft_note": "" if self.state == "tailored" else "The writing model could not be used (down)."}
        pipeline._write_json(folder / "result.json", result)
        return result


@pytest.fixture
def made(db, monkeypatch, tmp_path):
    """The person's resume (a real DOCX), no lines added, and the stand-in run."""
    from docx import Document
    resume = tmp_path / "master.docx"
    d = Document()
    for line in ("JORDAN EXAMPLE", "jordan@example.com", "EXPERIENCE", "Platform Engineer, Globex, 2020 - Present",
                 "Built Terraform modules for AWS", "SKILLS", "Terraform, Kubernetes, AWS"):
        d.add_paragraph(line)
    d.save(resume)
    monkeypatch.setattr("ml.resume.pipeline.base_resume_path", lambda cfg: resume)
    monkeypatch.setattr("ml.resume.additions.load", lambda *a, **k: [])
    monkeypatch.setattr("ml.resume.additions.ADDITIONS_PATH", tmp_path / "additions.yaml")
    monkeypatch.setattr("ml.resume.packet.packet_root", lambda cfg: tmp_path / "output")
    monkeypatch.setattr("ml.resume.packet.packet_dir", lambda cfg, *a, **k: tmp_path / "folders" / "job")
    runs = Runs()
    monkeypatch.setattr(pipeline, "run", runs)
    monkeypatch.setattr(runner, "say", lambda line: None)
    return SimpleNamespace(cfg=db[0], runs=runs, resume=resume, tmp=tmp_path)


def job(n=1):
    with get_session() as s:
        j = s.get(Job, n)
        s.expunge(j)
        return j


def versions(job_id=1):
    with get_session() as s:
        from sqlalchemy import text
        return [tuple(r) for r in s.execute(text(
            "SELECT version, state, source FROM document_publications WHERE job_id = :j ORDER BY version"), {"j": job_id})]


# -- every normal entry point uses it, and never the older engine ------------------------------------------------

@pytest.fixture
def no_older_engine(monkeypatch):
    def never(*a, **k):
        raise AssertionError("the older engine was used")
    monkeypatch.setattr("ml.resume.pipeline.tailor_jobs", never)
    monkeypatch.setattr("ml.resume.pipeline.tailor_to_target", never)


def test_every_normal_entry_point_goes_through_the_one_preparation(made, no_older_engine, monkeypatch, client):
    asked = []
    real = pipeline.prepare_for_job

    def recorded(cfg, j, **kw):
        asked.append(kw["purpose"])
        return real(cfg, j, **kw)
    monkeypatch.setattr(pipeline, "prepare_for_job", recorded)
    # Prepare now
    assert prepare._tailor(made.cfg, job(1))["run"]
    # Prepare these / Apply to these / a morning pick's Prepare: all through runner.choose_resume
    path, note = runner.choose_resume(made.cfg, job(2))
    assert path == co.current(2).pdf and note == "tailored just now"
    # the command line, `tailor --job-id`
    import main
    args = main.build_parser().parse_args(["tailor", "--job-id", "5"])
    assert not args.legacy and main._tailor_canonical(made.cfg, [5], again=False, paid_ok=False) == 0
    # the job page's Tailor
    c, _ = client
    monkeypatch.setattr("backend.api.app.cfg", lambda: made.cfg)
    r = c.post("/job/6/tailor-full", json={})
    assert r.status_code == 200
    for _ in range(200):
        if co.current(6) is not None:
            break
        threading.Event().wait(0.05)
    assert co.current(6) is not None
    assert asked == ["Prepare now", "Prepare these", "tailor (command line)", "the job page's Tailor"]


def test_the_older_engine_is_reached_only_by_legacy(made, monkeypatch, capsys):
    import main
    used = []
    monkeypatch.setattr("ml.resume.pipeline.tailor_to_target", lambda cfg, j, again=False: used.append(j) or ([], None))
    args = main.build_parser().parse_args(["tailor", "--legacy", "--job-id", "1"])
    monkeypatch.setattr(main, "init_engine", lambda url: None)
    made.cfg.database_url = "sqlite:///unused"            # the command line's own settings carry it
    main.cmd_tailor(made.cfg, args)
    assert used == [1] and "older tailoring engine" in capsys.readouterr().out
    assert main._tailor_canonical(made.cfg, [1, 2], again=False, paid_ok=False) == 2       # batches: legacy only


def test_a_chosen_jobs_run_records_the_published_files(made, no_older_engine):
    execution.add([1])
    runner.prepare_chosen(made.cfg)
    p = co.current(1)
    with get_session() as s:
        art = s.query(ApplyArtifact).filter(ApplyArtifact.item_id == item_of(1).id, ApplyArtifact.state == "ready").one()
        assert art.sha256 == p.pdf_sha256 and art.origin_sha256 == p.pdf_sha256   # the very published PDF
    assert item_of(1).state == QUEUED and "tailored just now" in item_of(1).note


# -- finished, failed, unfinished -------------------------------------------------------------------------------

def test_an_unfinished_or_crashed_run_is_never_taken_for_done(made):
    folder = pipeline.new_run()                         # a run that started and never finished: no result.json
    pipeline._write_json(pipeline.JOBS / "1.json", {"run": folder.name, "best": folder.name, "best_score": 99})
    assert pipeline.job_best(1) == (None, None)        # until Nov 2026 this answered with that run
    done = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    assert not done.reused and made.runs.calls == 1 and done.publication.version == 1


@pytest.mark.parametrize("state", ["writer_unavailable", "rewrite_rejected"])
def test_a_draft_that_was_not_tailored_is_never_published_as_if_it_were(made, state):
    made.runs.state = state
    with pytest.raises(pipeline.TailoringFailed) as failed:
        pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    assert failed.value.kind == "not_tailored" and "choose your own resume" in failed.value.message
    assert co.current(1) is None and versions() == []


def test_a_failed_tailoring_gives_its_reason_and_nothing_takes_its_place(made, no_older_engine, monkeypatch):
    made.runs.raise_at = pipeline.STAGES[2]
    monkeypatch.setattr(runner, "selected_master", lambda cfg, j: (_ for _ in ()).throw(AssertionError("never")))
    path, note = runner.choose_resume(made.cfg, job(1))
    assert path is None and "the writer stopped answering" in note
    execution.add([1])
    runner.prepare_chosen(made.cfg)
    item = item_of(1)
    assert item.state == FAILED and "resume was not made" in item.outcome and "your own resume" in item.outcome
    view = next(i for i in execution.view(made.cfg)["items"] if i["id"] == item.id)
    assert view["can"]["own"] and view["can"]["retry"]
    with get_session() as s:
        assert s.query(ApplyArtifact).filter(ApplyArtifact.item_id == item.id).count() == 0


@pytest.mark.parametrize("kind, why", [("no_posting", "teaser"), ("no_resume", "base resume")])
def test_missing_inputs_fail_with_their_reason(made, monkeypatch, kind, why):
    if kind == "no_posting":
        with get_session() as s:
            s.get(Job, 1).description = "Run Azure."
    else:
        monkeypatch.setattr("ml.resume.pipeline.base_resume_path",
                            lambda cfg: (_ for _ in ()).throw(FileNotFoundError("No base resume found.")))
    with pytest.raises(pipeline.TailoringFailed) as failed:
        pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    assert failed.value.kind == kind and why in failed.value.message.lower()


# -- the repeat key ---------------------------------------------------------------------------------------------

def test_the_same_inputs_are_not_made_twice_and_each_change_makes_it_anew(made, monkeypatch):
    first = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    again = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare these")
    assert again.reused and again.publication.id == first.publication.id and made.runs.calls == 1
    with get_session() as s:                            # the posting changes
        s.get(Job, 1).description = "x " * 400 + "and Kubernetes"
    assert not pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now").reused and made.runs.calls == 2
    (made.tmp / "additions.yaml").write_text("- line: I ran EKS\n")     # the person's evidence changes
    assert not pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now").reused and made.runs.calls == 3
    monkeypatch.setattr(pipeline, "RULES_VERSION", "a1-next")          # the rules change
    assert not pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now").reused and made.runs.calls == 4
    pipeline.mark_redo([1], "made from a master since repaired")      # marked to be made again
    assert not pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now").reused and made.runs.calls == 5
    assert pipeline.needs_redo(1) is None


def test_a_regenerated_document_waits_for_approval_again(made):
    first = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    with get_session() as s:
        s.add(DocumentApproval(job_id=1, kind="resume", sha256=first.publication.pdf_sha256, name="resume.pdf",
                               source="tailored", decision="approved"))
    assert approval.decision(1, "resume", first.pdf) == "approved"
    redo = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now", again=True)
    assert redo.publication.pdf_sha256 != first.publication.pdf_sha256
    assert approval.decision(1, "resume", redo.pdf) == ""                # never inherited
    assert approval.decision(1, "resume", first.pdf) == "approved"       # the old version keeps its word


def test_a_worse_rewrite_never_replaces_a_better_one(made, monkeypatch):
    first = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    monkeypatch.setattr(pipeline, "_better", lambda job_id, folder, score: False)
    kept = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now", again=True)
    assert kept.reused and kept.publication.id == first.publication.id and "matches better" in kept.note


# -- one at a time, fenced, fail closed ------------------------------------------------------------------------

def test_a_job_being_prepared_elsewhere_is_said_to_be_busy(made):
    held = co.acquire(1, "Prepare these")
    with pytest.raises(pipeline.TailoringFailed) as failed:
        pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    assert failed.value.kind == "busy" and made.runs.calls == 0
    co.release(held)


def test_a_damaged_claim_stops_preparation_instead_of_doubling_it(made):
    co.release(co.acquire(1, "earlier"))
    import sqlite3
    raw = sqlite3.connect(made.tmp / "jobs.db")
    raw.execute("UPDATE preparation_leases SET token = 'x'")
    raw.commit()
    raw.close()
    with pytest.raises(pipeline.TailoringFailed) as failed:
        pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    assert failed.value.kind == "claim_unreadable" and made.runs.calls == 0


def test_a_worker_that_lost_the_job_while_tailoring_publishes_nothing(made):
    def taken_over():
        co.acquire(1, "Prepare now", now=co._now() + timedelta(seconds=co.LEASE_TTL_S + 5))
    made.runs.during = {pipeline.STAGES[4]: taken_over}
    with pytest.raises(pipeline.TailoringFailed) as failed:
        pipeline.prepare_for_job(made.cfg, job(1), purpose="a run that hung")
    assert failed.value.kind == "claim_lost" and co.current(1) is None


@pytest.mark.parametrize("stage", list(range(6)))
def test_a_cancel_at_each_stage_publishes_nothing_and_frees_the_job(made, stage):
    told = {"now": False}
    made.runs.during = {pipeline.STAGES[stage]: lambda: told.update(now=True)}
    with pytest.raises(pipeline.TailoringCancelled):
        pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare these", cancelled=lambda: told["now"])
    assert co.current(1) is None and versions() == []
    co.release(co.acquire(1, "next"))                                   # the claim was given back


def test_a_job_cancelled_while_its_documents_were_made_gets_none_recorded(made):
    execution.add([1])
    item = item_of(1).id

    def cancel_meanwhile():
        with get_session() as s:
            from data_engineering.db.models import ApplyItem
            s.get(ApplyItem, item).state = CANCELLED
    made.runs.during = {pipeline.STAGES[5]: cancel_meanwhile}
    runner.prepare_chosen(made.cfg)
    assert item_of(1).state == CANCELLED and co.current(1) is None
    with get_session() as s:
        assert s.query(ApplyArtifact).filter(ApplyArtifact.item_id == item).count() == 0


CHILD = r"""
import sys, time, json
sys.path.insert(0, {here!r})
from types import SimpleNamespace
from data_engineering.db.session import init_engine, get_session
init_engine({url!r})
from ml.tailoring import coordination as co, pipeline
co.PUBLISHED = __import__("pathlib").Path({published!r})
pipeline.RUNS = __import__("pathlib").Path({runs!r}); pipeline.JOBS = __import__("pathlib").Path({jobs!r})
pipeline.REDO = __import__("pathlib").Path({redo!r})
import ml.resume.pipeline as rp, ml.resume.additions as ad, ml.resume.packet as pk
rp.base_resume_path = lambda cfg: __import__("pathlib").Path({resume!r})
ad.load = lambda *a, **k: []
ad.ADDITIONS_PATH = __import__("pathlib").Path({tmp!r}) / "additions.yaml"   # never the person's own file
pk.packet_root = lambda cfg: __import__("pathlib").Path({tmp!r}) / "output"
pk.packet_dir = lambda cfg, *a, **k: __import__("pathlib").Path({tmp!r}) / "folders" / "job"
def slow(folder, resume, jd="", jf=None, cfg=None, progress=lambda s, n="": None, company=None, **kw):
    for st in pipeline.STAGES:
        progress(st); time.sleep(0.3)
    (folder / "tailored-resume.docx").write_bytes(b"DOCX " * 300); (folder / "tailored-resume.pdf").write_bytes(b"%PDF " * 300)
    r = {{"score": 80.0, "before_after": {{"ats": [60, 81]}}, "draft_state": "tailored", "draft_note": ""}}
    (folder / "result.json").write_text(json.dumps(r)); return r
pipeline.run = slow
from data_engineering.db.models import Job
with get_session() as s:
    j = s.get(Job, 1); s.expunge(j)
while time.time() < {start}:
    time.sleep(0.001)
class C(dict):
    def get(self, k, d=None): return super().get(k, d)
try:
    pipeline.prepare_for_job(C(), j, purpose="process")
    print("made")
except pipeline.TailoringFailed as e:
    print(e.kind)
"""


def test_processes_preparing_one_job_at_once_make_it_once(made):
    import time
    start = time.time() + 3
    script = CHILD.format(here=HERE, url=f"sqlite:///{made.tmp / 'jobs.db'}", published=str(co.PUBLISHED),
                          runs=str(pipeline.RUNS), jobs=str(pipeline.JOBS), redo=str(pipeline.REDO),
                          resume=str(made.resume), tmp=str(made.tmp), start=start)
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(3)]
    outs = [p.communicate(timeout=120) for p in procs]
    said = sorted(o.strip().splitlines()[-1] if o.strip() else f"error: {e[-300:]}" for o, e in outs)
    assert said == ["busy", "busy", "made"], said
    assert [v[:2] for v in versions()] == [(1, "published")]


def test_threads_preparing_one_job_at_once_make_it_once(made):
    results, gate = [], threading.Barrier(4)

    def go():
        gate.wait()
        try:
            results.append("reused" if pipeline.prepare_for_job(made.cfg, job(1), purpose="t").reused else "made")
        except pipeline.TailoringFailed as exc:
            results.append(exc.kind)
    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("made") == 1 and made.runs.calls == 1 and set(results) <= {"made", "busy", "reused"}


def test_after_a_restart_half_made_versions_are_settled_and_never_shown(made):
    lease = co.acquire(1, "dies")
    docx, pdf = made.tmp / "a.docx", made.tmp / "a.pdf"
    docx.write_bytes(b"D" * 100)
    pdf.write_bytes(b"P" * 100)

    def die(stage):
        if stage == "pdf":
            raise co._Crash()
    with pytest.raises(co._Crash):
        co.publish(lease, docx=docx, pdf=pdf, source="tailored", repeat_key="k", run_id="r", crash=die)
    co.release(lease)                                   # its process "is gone": the claim is free
    assert co.current(1) is None
    execution.recover()
    assert versions() == [(1, "failed", "tailored")]


# -- the person's own resume, only on their press ------------------------------------------------------------

def test_use_my_own_resume_is_explicit_and_goes_back_in_the_queue(made, client, monkeypatch):
    c, _ = client
    monkeypatch.setattr("backend.api.chosen_pages.cfg", lambda: made.cfg)
    made.runs.raise_at = pipeline.STAGES[1]
    execution.add([1])
    runner.prepare_chosen(made.cfg)
    item = item_of(1)
    assert item.state == FAILED and co.current(1) is None              # nothing chosen for the person
    r = c.post("/apply/chosen/own-resume", json={"item": item.id})
    assert r.status_code == 200 and r.get_json()["version"] == 1
    p = co.current(1)
    assert p.source == "own_resume" and item_of(1).state == QUEUED
    path, note = runner.choose_resume(made.cfg, job(1))
    assert path == p.pdf and note == "your own resume, chosen by you"
    assert execution.documents_unready(1, p.pdf, None) == "its resume is your own, chosen by you, not a checked tailoring"
    steps = [e["message"] for e in execution.events_after(0, item_id=item.id)]
    assert any(m.startswith("You chose your own resume for this job") for m in steps)
    assert c.post("/apply/chosen/own-resume", json={"item": item.id}).status_code == 409   # only for a failed one


# -- readers, edits, spending ----------------------------------------------------------------------------------

def test_every_reader_sees_the_published_version_not_the_folder_copy(made, monkeypatch):
    done = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    folder_copy = made.tmp / "output"
    for f in folder_copy.glob("*.pdf"):                 # someone changes the job folder's copy
        os.chmod(f, 0o644)
        f.write_bytes(b"%PDF tampered copy")
    from backend.api import app as webapp
    monkeypatch.setattr(webapp, "cfg", lambda: made.cfg)
    from ml.resume import ats, recheck
    assert runner.tailored_resume(made.cfg, job(1)) == done.pdf
    assert webapp.job_documents(1)["resume_pdf"] == done.pdf
    assert ats.tailored_pdf(made.cfg, job(1)) == done.pdf
    assert recheck._files(made.cfg, job(1)) == (done.pdf, done.docx)


def test_an_edit_to_a_jobs_run_becomes_a_new_version_that_waits_for_approval(made):
    done = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    folder = pipeline.folder_of(done.run_id)
    os.chmod(folder / "tailored-resume.pdf", 0o644)
    (folder / "tailored-resume.pdf").write_bytes(b"%PDF edited by the person " * 50)
    pipeline.publish_edit(1, folder, note="edited by you")
    now = co.current(1)
    assert now.version == 2 and now.pdf_sha256 != done.publication.pdf_sha256
    assert approval.decision(1, "resume", now.pdf) == ""


def test_a_writer_that_bills_needs_a_yes_and_a_limit_and_every_call_counts(made, monkeypatch):
    monkeypatch.setattr(runner, "writer", lambda cfg: ("fakebill", "A billing writer", False))
    with pytest.raises(pipeline.TailoringFailed) as failed:
        pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    assert failed.value.kind == "paid_not_allowed"
    with pytest.raises(pipeline.TailoringFailed) as failed:
        pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now", paid_ok=True)
    assert failed.value.kind == "no_spend_limit"
    # With a limit: every billed call of the preparation counts, those in threads too, and the one that
    # would pass the limit is never made.
    made.cfg["resume.max_spend_per_run_usd"] = 0.05
    monkeypatch.setitem(llm.PROVIDERS, "fakebill", {"label": "A billing writer", "free": False})
    monkeypatch.setattr(llm, "provider_name", lambda cfg=None: "fakebill")
    monkeypatch.setattr(llm, "model_name", lambda cfg=None, **k: "fake-model")
    monkeypatch.setattr(cost, "pricing_for", lambda model, cfg=None: {"input": 1000.0, "output": 1000.0})
    sent = []

    def dispatch(provider, system, user, cfg, **kw):
        sent.append(provider)
        return llm.Completion("{}", "fake-model", provider, SimpleNamespace(input_tokens=10, output_tokens=10))
    monkeypatch.setattr(llm, "_dispatch", dispatch)

    def calls_the_writer():
        llm.complete("s", "u", made.cfg)
        threading.Thread(target=lambda: _quiet(llm.complete, "s", "u" * 20000, made.cfg)).start()
    made.runs.during = {pipeline.STAGES[2]: calls_the_writer}
    with pytest.raises(pipeline.TailoringFailed) as failed:
        pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now", paid_ok=True)
    assert failed.value.kind in ("spend_limit", "error") and co.current(1) is None
    assert sent == []                                   # priced above the limit: not one call was made


def _quiet(fn, *a):
    try:
        fn(*a)
    except cost.SpendCapReached:
        pass


def test_legacy_tailored_runs_are_adopted_without_a_model_when_unchanged(made):
    folder = pipeline.new_run()
    pipeline.save_saved_resume(folder, made.resume)
    j = job(1)
    (folder / "inputs" / "job.txt").write_text(f"{j.title}\n\n{j.description or ''}")   # made for this very posting
    (folder / "tailored-resume.docx").write_bytes(b"DOCX old " * 300)
    (folder / "tailored-resume.pdf").write_bytes(b"%PDF old " * 300)
    pipeline._write_json(folder / "result.json", {"draft_note": "", "checks_ok": True})
    pipeline._write_json(pipeline.JOBS / "1.json", {"run": folder.name, "best": folder.name, "best_score": 88.0,
                                                    "measure": "ats"})
    done = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare these")
    assert done.reused and made.runs.calls == 0 and done.publication.run_id == folder.name
    assert co.current(1).pdf.read_bytes() == (folder / "tailored-resume.pdf").read_bytes()


def test_a_cancel_after_the_resume_is_made_but_before_it_is_recorded_records_nothing(made, monkeypatch):
    """The last gap: the resume is published, and the person cancels before the entry's documents are written
    down. The entry stays cancelled and has no documents (record_documents checks its state as it writes)."""
    execution.add([1])
    item = item_of(1).id
    real = runner.prepare_documents

    def made_then_cancelled(*a, **kw):
        done = real(*a, **kw)
        with get_session() as s:
            from data_engineering.db.models import ApplyItem
            s.get(ApplyItem, item).state = CANCELLED
        return done
    monkeypatch.setattr(runner, "prepare_documents", made_then_cancelled)
    runner.prepare_chosen(made.cfg)
    assert item_of(1).state == CANCELLED and co.current(1) is not None
    with get_session() as s:
        assert s.query(ApplyArtifact).filter(ApplyArtifact.item_id == item).count() == 0
