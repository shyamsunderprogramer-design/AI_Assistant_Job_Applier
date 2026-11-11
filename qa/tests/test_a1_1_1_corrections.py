"""A1.1.1 (10 Nov 2026): the activation blockers the A1.1 final verification found, each with a test that fails
without its fix. (The writers verified to cost nothing per use: test_writer_approval.py. Local dates: test_local_dates.py.)

  * "Use the earlier one" is kept: every later Prepare -- whatever template, however often, from several processes
    at once -- uses the version the person chose, until they ask for a new one.
  * The app presses Submit by itself only if the resume attached is still the job's current, approved version a
    moment before it does; a version published while the form is open leaves Submit to the person.
  * An unexpected error while a job is made ready ends it Failed, with nothing sent, and the run goes on; one
    after its form opened is never guessed: it waits for the person's word, and is never tried again by itself.

Made-up jobs in a database of the test's own, the tailoring run and the employer's form stood in for."""

import subprocess
import sys

import pytest

from backend.apply import execution, greenhouse, runner
from backend.apply.execution import FAILED, PENDING, QUEUED
from ml.tailoring import coordination as co
from ml.tailoring import pipeline
from qa.tests.test_a1_1_corrections import _earlier, form  # noqa: F401
from qa.tests.test_canonical_preparation import CHILD, HERE, Runs, job, made, versions  # noqa: F401
from qa.tests.test_execution_queue import Context, client, db, item_of  # noqa: F401


# -- 2. "Use the earlier one" is kept ---------------------------------------------------------------------------

@pytest.fixture
def chosen(made, monkeypatch):
    """Job 1 is a posting whose own template is not the configured default (a startup's: "modern"), and the person
    chose its earlier tailored resume, made from other inputs, as it is."""
    monkeypatch.setattr("ml.resume.style.for_job", lambda *a, **k: ("modern", "a startup's posting"))
    folder = _earlier(made, posting="an earlier version of the posting")
    published = pipeline.use_earlier_resume(made.cfg, job(1))
    return folder, published


def test_the_next_prepare_keeps_the_earlier_one_the_person_chose(made, chosen):
    folder, published = chosen
    done = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    assert done.reused and made.runs.calls == 0                        # no tailoring run over it
    assert done.publication.version == published.version == co.current(1).version
    assert done.publication.run_id == folder.name and "chosen by you" in done.note


def test_its_key_is_the_one_every_prepare_computes(made, chosen):
    _, published = chosen
    from ml.resume.pipeline import base_resume_path
    assert published.repeat_key == pipeline.repeat_key(made.cfg, job(1), base_resume_path(made.cfg),
                                                       template=pipeline.job_template(made.cfg, job(1)))


@pytest.mark.parametrize("template", ["classic", "federal", "modern"])
def test_a_template_chosen_later_does_not_replace_it(made, chosen, template):
    _, published = chosen
    done = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now", template=template)
    assert done.reused and made.runs.calls == 0 and co.current(1).version == published.version


def test_preparing_again_and_again_keeps_it(made, chosen):
    _, published = chosen
    for purpose in ("Prepare now", "Prepare these", "a morning pick's Prepare", "Prepare now"):
        assert pipeline.prepare_for_job(made.cfg, job(1), purpose=purpose).reused
    assert made.runs.calls == 0 and [v[0] for v in versions()] == [published.version]


def test_prepare_these_keeps_it_too(made, chosen):
    _, published = chosen
    execution.add([1])
    runner.prepare_chosen(made.cfg)
    assert made.runs.calls == 0 and co.current(1).version == published.version


def test_processes_preparing_at_once_all_keep_it(made, chosen):
    import time
    _, published = chosen
    start = time.time() + 3
    script = CHILD.format(here=HERE, url=f"sqlite:///{made.tmp / 'jobs.db'}", published=str(co.PUBLISHED),
                          runs=str(pipeline.RUNS), jobs=str(pipeline.JOBS), redo=str(pipeline.REDO),
                          resume=str(made.resume), tmp=str(made.tmp), start=start)
    script = script.replace("pipeline.run = slow", "pipeline.run = slow\nimport ml.resume.style as st\n"
                            "st.for_job = lambda *a, **k: ('modern', 'a startup')")
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(3)]
    outs = [p.communicate(timeout=120) for p in procs]
    said = sorted(o.strip().splitlines()[-1] if o.strip() else f"error: {e[-300:]}" for o, e in outs)
    assert all(s in ("made", "busy") for s in said), said              # "made": its prepare returned (reused)
    assert [v[0] for v in versions()] == [published.version]           # no new version from any of them
    assert co.current(1).run_id == chosen[0].name


@pytest.mark.parametrize("how", ["tailor it again", "re-tailor"])
def test_asking_for_a_new_one_ends_the_choice(made, chosen, how):
    _, published = chosen
    if how == "tailor it again":
        pipeline.mark_redo([1], "you asked for a new one")
        done = pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now")
    else:                     # the job page's Re-tailor replaces it when the new one matches at least as well
        record = pipeline._job_record(1)
        pipeline._write_json(pipeline.JOBS / "1.json", {**record, "best_score": 70.0})
        done = pipeline.prepare_for_job(made.cfg, job(1), purpose="the job page's Tailor", again=True)
    assert made.runs.calls == 1 and done.publication.version == published.version + 1
    assert not pipeline.chosen_by_person(1, co.current(1))
    assert pipeline.prepare_for_job(made.cfg, job(1), purpose="Prepare now").reused   # and the new one is kept


# -- 3. the resume attached is checked again a moment before the app presses Submit --------------------------

@pytest.fixture
def open_form(monkeypatch):
    """The employer's form, stood in for: while it is open, `meanwhile` happens; then the last check is asked,
    as Greenhouse asks it a moment before Submit."""
    seen = {"meanwhile": lambda: None}

    def fill_form(cfg, page, j, applicant, head, ready, *, auto, wait_s):
        seen["meanwhile"]()
        seen["last_check"] = ready.last_check() if ready.last_check is not None else "no last check"
        return runner.Outcome(j.id, j.company, j.title, "skipped", how="you")
    monkeypatch.setattr(runner, "fill_form", fill_form)
    monkeypatch.setattr(runner, "write_letter_if_missing", lambda cfg, j, head="": False)
    monkeypatch.setattr(runner, "cover_letter", lambda cfg, j, evidence: None)
    from backend.apply import prepare
    monkeypatch.setattr(prepare, "open_questions", lambda j, applicant: [])
    return seen


def _ready_then_open(made, open_form, meanwhile):
    execution.add([1])
    runner.prepare_chosen(made.cfg)
    open_form["meanwhile"] = meanwhile
    runner.run(made.cfg, chosen=True, browser_factory=Context)
    return open_form["last_check"]


def test_unchanged_while_open_it_may_go(made, open_form):
    said = _ready_then_open(made, open_form, lambda: None)
    assert said is None or "approval" in said or "approved" in said or "your word" in said   # only the person's word missing


def test_a_version_published_while_the_form_is_open_is_caught_before_submit(made, open_form):
    def tailored_again():
        pipeline.prepare_for_job(made.cfg, job(1), purpose="the job page's Tailor", again=True)
    said = _ready_then_open(made, open_form, tailored_again)
    assert said and "newer version of its resume (version 2)" in said


def test_withdrawn_while_open_is_caught(made, open_form):
    def withdrawn():
        from sqlalchemy import text
        from data_engineering.db.session import get_session
        with get_session() as s:
            s.execute(text("UPDATE document_publications SET state = 'withdrawn' WHERE job_id = 1"))
    said = _ready_then_open(made, open_form, withdrawn)
    assert said and "withdrawn" in said


def test_the_attached_copy_changed_while_open_is_caught(made, open_form):
    def changed():
        sent = next((p for p in (made.tmp).rglob("send/*.pdf")), None)
        sent.chmod(0o644)
        sent.write_bytes(b"%PDF something else " * 40)
    said = _ready_then_open(made, open_form, changed)
    assert said and "no longer the one checked" in said


def test_a_last_check_that_fails_is_never_a_yes(made, open_form, monkeypatch):
    def broken():
        monkeypatch.setattr(execution, "superseded", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    said = _ready_then_open(made, open_form, broken)
    assert said and "could not be checked again" in said


# Greenhouse itself: the one board the app ever presses Submit on.

def _page():
    from qa.tests.test_apply_runner import Page
    return Page()


def _person():
    from qa.tests.test_apply_runner import person
    return person()


def test_greenhouse_presses_submit_only_when_the_last_check_says_yes(tmp_path):
    resume = tmp_path / "r.pdf"
    resume.write_text("x")
    page = _page()
    result = greenhouse.fill(page, 1, "u", _person(), resume, submit=True, before_submit=lambda: None)
    assert result.submitted and page.submitted and result.held_back is None


@pytest.mark.parametrize("check", [
    lambda: "a newer version of its resume (version 2) was published after these documents were made ready",
    lambda: (_ for _ in ()).throw(RuntimeError("db gone")),
    None,
])
def test_greenhouse_leaves_submit_to_the_person_otherwise(tmp_path, check):
    resume = tmp_path / "r.pdf"
    resume.write_text("x")
    page = _page()
    kw = {"before_submit": check} if check is not None else {}
    result = greenhouse.fill(page, 1, "u", _person(), resume, submit=True, **kw)
    assert not result.submitted and not page.submitted and result.held_back


def test_the_app_never_submits_by_itself_without_a_last_check(monkeypatch, tmp_path):
    given = {}

    def fill(page, job_id, url, applicant, resume=None, letter=None, *, submit=False, evidence=None, **kw):
        given.update(submit=submit, **kw)
        return greenhouse.FillResult(job_id=job_id, url=url, error="stop here")
    monkeypatch.setattr(greenhouse, "fill", fill)
    from types import SimpleNamespace
    j = SimpleNamespace(id=1, company="Globex", title="Engineer", source="greenhouse", company_slug="globex",
                        external_id="1")
    ready = runner.Prepared(tmp_path / "r.pdf", None, "tailored", tmp_path)          # no last check
    runner.fill_form(None, _page(), j, _person(), "h", ready, auto=True, wait_s=1)
    assert given["submit"] is False and "before_submit" not in given
    check = lambda: None                                                                # noqa: E731
    runner.fill_form(None, _page(), j, _person(), "h", runner.Prepared(tmp_path / "r.pdf", None, "tailored",
                                                                       tmp_path, last_check=check), auto=True, wait_s=1)
    assert given["submit"] is True and given["before_submit"] is check


def test_other_boards_stay_the_persons_to_submit(monkeypatch, tmp_path):
    """A posting on another board that leads to a Greenhouse form: filled, never sent by the app."""
    from types import SimpleNamespace
    from backend.apply import anyform
    given = {}
    monkeypatch.setattr("backend.core.joblinks.apply_link", lambda j: j.application_url)
    monkeypatch.setattr(anyform, "reach", lambda page, start, **k: SimpleNamespace(
        stop=None, error=None, page=page, kind="greenhouse", url="https://boards.greenhouse.io/x/jobs/1"))
    monkeypatch.setattr(anyform, "greenhouse_form", lambda url: url)

    def fill(page, job_id, url, applicant, resume=None, letter=None, *, submit=False, evidence=None, **kw):
        given.update(submit=submit)
        return greenhouse.FillResult(job_id=job_id, url=url, error="stop here")
    monkeypatch.setattr(greenhouse, "fill", fill)
    j = SimpleNamespace(id=1, company="Initech", title="Engineer", source="jobvite", application_url="https://x/1",
                        company_slug="initech", external_id="1")
    ready = runner.Prepared(tmp_path / "r.pdf", None, "tailored", tmp_path, last_check=lambda: None)
    runner.fill_form(None, _page(), j, _person(), "h", ready, auto=True, wait_s=1)
    assert given["submit"] is False


# -- 5. failure recovery ---------------------------------------------------------------------------------------------

def _two_ready(made):
    execution.add([1, 4])
    runner.prepare_chosen(made.cfg)


def test_an_error_while_a_job_is_made_ready_fails_it_and_the_run_goes_on(made, form, monkeypatch):
    real = execution.superseded

    def first_job_breaks(job_id, artifact):
        if job_id == 1:
            raise RuntimeError("the database went away")
        return real(job_id, artifact)
    _two_ready(made)
    monkeypatch.setattr(execution, "superseded", first_job_breaks)
    runner.run(made.cfg, chosen=True, browser_factory=Context)               # no error out of the run
    first = item_of(1)
    assert first.state == FAILED and "Nothing was sent" in first.outcome and "database went away" in first.outcome
    assert [g["job"] for g in form] == [4]                                    # the next one still went


def test_an_error_checking_documents_before_the_form_fails_it_unsent(made, form, monkeypatch):
    _two_ready(made)
    real = runner.run_copy

    def plain_copy_fails(source, target):
        if target.name.startswith("resume") and "send" not in str(target.parent.name):
            raise OSError("no space left on the disk")
        return real(source, target)
    monkeypatch.setattr(runner, "run_copy", plain_copy_fails)
    runner.run(made.cfg, chosen=True, browser_factory=Context)
    for job_id in (1, 4):
        item = item_of(job_id)
        assert item.state == FAILED and "Nothing was sent" in item.outcome, (item.state, item.outcome)
    assert form == []


def test_an_error_with_the_form_open_waits_for_the_persons_word_and_is_never_retried(made, monkeypatch):
    from backend.apply import prepare
    monkeypatch.setattr(runner, "write_letter_if_missing", lambda cfg, j, head="": False)
    monkeypatch.setattr(runner, "cover_letter", lambda cfg, j, evidence: None)
    monkeypatch.setattr(prepare, "open_questions", lambda j, applicant: [])
    opened = []

    def fill_form(cfg, page, j, applicant, head, ready, *, auto, wait_s):
        opened.append(j.id)
        execution.move(item_of(j.id).id, execution.WAITING, note="Waiting for you", expect=execution.RUNNING)
        raise RuntimeError("the browser crashed with the form open")
    monkeypatch.setattr(runner, "fill_form", fill_form)
    _two_ready(made)
    with pytest.raises(RuntimeError):
        runner.run(made.cfg, chosen=True, browser_factory=Context)
    first = item_of(1)
    assert first.state == PENDING and "may have been sent" in (first.action or "")
    execution.recover()
    assert item_of(1).state == PENDING                                          # recovery never guesses
    with pytest.raises(RuntimeError):
        runner.run(made.cfg, chosen=True, browser_factory=Context)            # the next run: job 4's form
    assert opened == [1, 4] and item_of(1).state == PENDING                    # job 1 never opened again


def test_an_error_while_preparing_these_fails_that_one_and_goes_on(made, monkeypatch):
    real = execution._prepare
    calls = []

    def first_breaks(cfg, applicant, run_id, item_id, *a, **k):
        calls.append(item_id)
        if len(calls) == 1:
            execution.move(item_id, execution.PREPARING, note="Making it ready", expect=execution.VALIDATING)
            raise OSError("no space left on the disk")
        return real(cfg, applicant, run_id, item_id, *a, **k)
    monkeypatch.setattr(execution, "_prepare", first_breaks)
    execution.add([1, 4])
    runner.prepare_chosen(made.cfg)
    failed = [i for i in (item_of(1), item_of(4)) if i.state == FAILED]
    assert len(calls) == 2 and len(failed) == 1 and "Nothing was sent" in failed[0].outcome
    assert {item_of(1).state, item_of(4).state} == {FAILED, QUEUED}


def test_a_re_tailor_that_matches_less_well_keeps_the_choice_and_says_so(made, chosen):
    _, published = chosen                                       # the chosen one scored 88; the new one 81
    done = pipeline.prepare_for_job(made.cfg, job(1), purpose="the job page's Tailor", again=True)
    assert made.runs.calls == 1 and done.reused and "earlier one matches better: kept" in done.note
    assert co.current(1).version == published.version and pipeline.chosen_by_person(1, co.current(1))


def test_a_choice_that_cannot_be_written_down_is_said_and_never_called_unpublished(made, monkeypatch):
    _earlier(made, posting="an earlier version of the posting")
    real = pipeline._write_json

    def no_room_for_the_choice(path, data):
        if "chosen_version" in data:
            raise OSError("no space left on the disk")
        return real(path, data)
    monkeypatch.setattr(pipeline, "_write_json", no_room_for_the_choice)
    with pytest.raises(pipeline.TailoringFailed) as caught:
        pipeline.use_earlier_resume(made.cfg, job(1))
    assert caught.value.kind == "choice_not_saved" and "version 1" in caught.value.message
    assert co.current(1).version == 1                                   # published, and said so
