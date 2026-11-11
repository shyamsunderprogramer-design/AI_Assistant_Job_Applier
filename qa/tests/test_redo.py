"""A tailored resume made from a master since repaired is made again before it is sent."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.apply import prepare, runner
from ml.tailoring import pipeline

JOB = SimpleNamespace(id=11127, company="Acme", title="Cloud Engineer", external_id="x", description="Run Azure.")
WHY = "made from your resume before its repair"


@pytest.fixture(autouse=True)
def redo_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "REDO", tmp_path / "redo.json")
    monkeypatch.setattr(pipeline, "JOBS", tmp_path / "jobs")
    return tmp_path


def test_a_marked_job_says_why_until_a_new_version_takes_its_place(redo_file, monkeypatch):
    pipeline.mark_redo([11127, 11498], WHY)
    assert pipeline.needs_redo(11127) == WHY and pipeline.needs_redo(7) is None
    # A rewrite placed as the job's resume clears the mark; one that is not placed keeps it.
    monkeypatch.setattr(pipeline, "place_for_job", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "source_of", lambda folder: "new")
    folder = redo_file / "runs" / "r2"
    folder.mkdir(parents=True)
    assert pipeline.finish_job_run(11127, folder, redo_file / "out.docx", 80.0)
    assert pipeline.needs_redo(11127) is None and pipeline.needs_redo(11498) == WHY


def test_auto_apply_never_attaches_the_old_file(redo_file, monkeypatch):
    """A marked job's old file is never attached: it is made again first, by the one canonical
    preparation; and when that fails, nothing takes its place by itself (Track A1, Nov 2026: until then
    the person's own lines went instead, silently)."""
    old = redo_file / "old.pdf"
    old.write_text("made from the damaged master")
    new = redo_file / "new.pdf"
    new.write_text("made again")
    monkeypatch.setattr(runner, "tailored_resume", lambda cfg, job: old)
    monkeypatch.setattr(runner, "selected_master", lambda cfg, job: (_ for _ in ()).throw(AssertionError("never")))
    pipeline.mark_redo([JOB.id], WHY)
    asked = []

    def made_again(cfg, job, **kw):
        asked.append(kw["again"])
        pipeline.redo_done(job.id)
        return SimpleNamespace(pdf=new, note="tailored just now")
    monkeypatch.setattr(pipeline, "prepare_for_job", made_again)
    assert runner.choose_resume(None, JOB) == (new, "tailored just now") and asked == [True]

    # Tailoring fails: the reason comes back, and no resume goes -- not the damaged file, not a substitute.
    pipeline.mark_redo([JOB.id], WHY)

    def fails(cfg, job, **kw):
        raise pipeline.TailoringFailed("error", "the writer did not answer")
    monkeypatch.setattr(pipeline, "prepare_for_job", fails)
    resume, note = runner.choose_resume(None, JOB)
    assert resume is None and "the writer did not answer" in note
    resume, note = runner.choose_resume(None, JOB, tailor=False)
    assert resume is None and "does not tailor" in note


def test_the_morning_preparation_tailors_a_marked_job_again(monkeypatch):
    """Prepare now goes through the one canonical preparation, which reuses a resume made for unchanged
    inputs and makes a marked one again (its own tests: qa/tests/test_canonical_preparation.py)."""
    calls = []

    def prepared(cfg, job, **kw):
        calls.append(kw)
        return SimpleNamespace(run_id="old-run", score=87.0)
    monkeypatch.setattr(pipeline, "prepare_for_job", prepared)
    assert prepare._tailor(None, JOB) == {"run": "old-run", "ats": 87.0}
    assert prepare._tailor(None, JOB, again=True) == {"run": "old-run", "ats": 87.0}
    assert [c["again"] for c in calls] == [False, True] and calls[0]["purpose"] == "Prepare now"
