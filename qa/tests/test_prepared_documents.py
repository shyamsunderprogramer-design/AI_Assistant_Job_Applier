"""Auto-apply Phase 3, step 2 (26 Oct 2026): Prepare these, and exactly that file. Each chosen job's
resume and cover letter are made ready and written down -- the file, its checksum, the one it was made
from, the run and the attempt -- with no form opened; a run gives each form exactly that content,
checked against the checksum when the form opens, and never sends one changed since; documents made
ready are never made again; and a writer that bills per use writes only once the person says yes.
Made-up jobs and files, a temporary database and a fake Greenhouse form: no browser, model or network."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.schema import CreateTable

from backend.apply import execution, greenhouse, runner
from backend.apply.execution import APPLIED, FAILED, PREPARING, QUEUED, READY, SKIPPED, VALIDATING
from backend.apply.profile import Applicant
from backend.core import eligibility
from data_engineering.db import session as session_mod
from data_engineering.db.models import ApplyArtifact, ApplyEvent, ApplyItem, ApplyRun, Job
from data_engineering.db.session import get_session, init_engine
from ml.resume import llm
from qa.tests.test_apply_greenhouse import FakePage
from qa.tests.test_eligibility_decisions import PLAIN, posted
from qa.tests.test_execution_hardening import engine_back, old_database  # noqa: F401
from qa.tests.test_execution_queue import client, db, dead_pid, item_of  # noqa: F401


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dana(stop_before_submit=True, **authorisation) -> Applicant:
    """A made-up person, whole enough for the Greenhouse filler."""
    return Applicant(personal={"first_name": "Dana", "last_name": "Example", "email": "dana@example.com",
                               "phone": "+1 555 0100"},
                     authorisation={"authorised_to_work": True, "requires_sponsorship": False, **authorisation},
                     apply={"stop_before_submit": stop_before_submit})


class Desk:
    """Each made-up job's own documents -- its tailored resume and its cover letter, where the job's folder
    keeps them -- and a prepare_documents that copies them under the employer's names into the attempt's
    folder, as the real one does, counting each time it is asked."""

    def __init__(self, root: Path):
        self.root, self.calls = root, []
        self.letters, self.checked = True, False      # a cover letter for each; no checked tailoring run

    def own(self, job_id, kind) -> Path:
        return self.root / "jobs" / str(job_id) / f"{kind}.pdf"

    def write(self, job_id, kind, words) -> Path:
        path = self.own(job_id, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(words.encode())
        return path

    def prepare_documents(self, cfg, job, applicant, head, *, tailor):
        self.calls.append(job.id)
        for kind in ("resume", "letter"):
            if not self.own(job.id, kind).exists():
                self.write(job.id, kind, f"%PDF-1.4 the {kind} made for job {job.id}, call {len(self.calls)}")
        evidence = greenhouse._evidence_dir(job.id)
        (evidence / "send").mkdir(parents=True, exist_ok=True)
        kept = evidence / "send" / f"Dana_Example_Resume_{job.company.replace(' ', '_')}.pdf"
        letter = evidence / "send" / f"Dana_Example_Cover_Letter_{job.company.replace(' ', '_')}.pdf"
        kept.write_bytes(self.own(job.id, "resume").read_bytes())
        letter.write_bytes(self.own(job.id, "letter").read_bytes())
        if self.checked:
            checked_run(job.id, self.own(job.id, "resume"))
        if not self.letters:
            return runner.Prepared(kept, None, "tailored", evidence, {"resume": self.own(job.id, "resume")})
        return runner.Prepared(kept, letter, "tailored", evidence,
                               {"resume": self.own(job.id, "resume"), "cover_letter": self.own(job.id, "letter")})


def checked_run(job_id: int, resume: Path, *, ok: bool = True, note: str | None = None) -> str:
    """A made-up tailoring run that made `resume` and checked it -- every check passed unless `ok` is False --
    as the job's best: what lets a run send it by itself (execution.documents_unready)."""
    from ml.tailoring import pipeline as tailoring
    run = f"run-for-{job_id}"
    folder = tailoring.RUNS / run
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tailored-resume.pdf").write_bytes(Path(resume).read_bytes())
    (folder / "result.json").write_text(json.dumps({"checks_ok": ok, "checks": [], "draft_note": note}))
    tailoring.JOBS.mkdir(parents=True, exist_ok=True)
    (tailoring.JOBS / f"{job_id}.json").write_text(json.dumps({"best": run}))
    return run


class Form(FakePage):
    """A Greenhouse form that takes a resume and a cover letter: what it is given, the employer gets."""

    def __init__(self):
        super().__init__(fields=["first_name", "last_name", "email", "phone", "input#resume", "input#cover_letter"],
                         captcha=False)


@pytest.fixture
def desk(db, monkeypatch, tmp_path):
    """The made-up documents, a folder per attempt, a person who presses Submit on each form, and no
    writer that bills: the run is as it is on this Mac, with every outside part made up."""
    made = Desk(tmp_path / "desk")
    monkeypatch.setattr(runner, "prepare_documents", made.prepare_documents)
    folders = iter(range(1, 1000))
    monkeypatch.setattr(greenhouse, "_evidence_dir", lambda job_id: _folder(tmp_path / "runs" / f"{job_id}-{next(folders)}"))
    monkeypatch.setattr(greenhouse, "watch_submission", lambda page, timeout_s, **kw: ("submitted", {}))
    monkeypatch.setattr(runner, "load", lambda path: dana())
    monkeypatch.setattr(eligibility, "sponsor_history", lambda company, country="US": None)
    monkeypatch.setenv("RESUME_PROVIDER", "claude-code")
    Browser.forms = []                                        # what this test's forms were given, only
    return made


def _folder(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


class Browser:
    """The app's browser: a new made-up form for each job, kept to see what each was given."""
    forms: list = []

    def new_page(self):
        Browser.forms.append(Form())
        return Browser.forms[-1]


def recorded(job_id) -> dict:
    """The documents written down for this job and not replaced since: made ready, or sent."""
    with get_session() as s:
        rows = (s.query(ApplyArtifact).filter(ApplyArtifact.item_id == item_of(job_id).id,
                                              ApplyArtifact.state.in_(("ready", "sent"))).order_by(ApplyArtifact.id))
        return {r.kind: execution._artifact_dict(r) for r in rows}


def last_run() -> ApplyRun:
    with get_session() as s:
        run = s.query(ApplyRun).order_by(ApplyRun.id.desc()).first()
        s.expunge(run)
        return run


def steps(job_id) -> list[str]:
    with get_session() as s:
        return [e.message for e in s.query(ApplyEvent).filter(ApplyEvent.job_id == job_id).order_by(ApplyEvent.id)]


# -- Prepare these -----------------------------------------------------------------------------------

def test_prepare_these_makes_each_one_ready_writes_down_each_file_and_sends_nothing(db, desk, monkeypatch):
    cfg, _, _ = db
    monkeypatch.setattr(runner, "fill_form", lambda *a, **k: pytest.fail("Prepare these opened a form"))
    monkeypatch.setattr(runner, "open_browser", lambda *a, **k: pytest.fail("Prepare these opened a browser"))
    posted(5, PLAIN + " Must be a US citizen.")             # unanswered on the profile: waits
    execution.add([3, 2, 1, 4, 5])
    with get_session() as s:
        s.get(Job, 3).is_open = False                       # closed since: it cannot go
    assert runner.prepare_chosen(cfg) == 3
    assert desk.calls == [1, 2, 4]                          # best fit first; Globex's two both made ready
    run = last_run()
    assert (run.state, run.note) == ("done", "Done: 3 made ready, nothing sent. Apply to these gives each form "
                                             "exactly those files.")
    states = {j: item_of(j).state for j in (1, 2, 3, 4, 5)}
    assert states == {1: QUEUED, 2: QUEUED, 3: SKIPPED, 4: QUEUED, 5: QUEUED}
    assert item_of(3).outcome == "Can't go: the posting has closed."
    assert item_of(1).note == "Made ready (tailored): exactly these files go with its form"
    assert recorded(5) == {} and item_of(5).decision == eligibility.NEEDS_INFORMATION
    for job_id in (1, 2, 4):
        docs = recorded(job_id)
        assert set(docs) == {"resume", "cover_letter"}
        for kind, own in (("resume", "resume"), ("cover_letter", "letter")):
            d = docs[kind]
            assert d["sha256"] == sha(d["path"]) == sha(desk.own(job_id, own))       # the file, and its checksum
            assert d["origin"] == str(desk.own(job_id, own)) and d["origin_sha256"] == sha(desk.own(job_id, own))
            assert (d["run_id"], d["attempt"], d["state"], d["source"] if kind == "resume" else "") == (
                run.id, 1, "ready", "tailored" if kind == "resume" else "")
    written = [s for s in steps(1) if s.startswith("Documents ready")]
    assert written == [f"Documents ready: resume Dana_Example_Resume_Globex.pdf (checksum {recorded(1)['resume']['sha256'][:12]}) "
                       f"and cover letter Dana_Example_Cover_Letter_Globex.pdf (checksum "
                       f"{recorded(1)['cover_letter']['sha256'][:12]}) · tailored"]


def test_documents_made_ready_are_never_made_again(db, desk, client):
    cfg, _, _ = db
    c, started = client
    execution.add([1, 6])
    assert runner.prepare_chosen(cfg) == 2 and runner.prepare_chosen(cfg) == 0
    assert sorted(desk.calls) == [1, 6]
    view = c.get("/apply/chosen").get_json()
    assert view["to_prepare"] == 0
    reply = c.post("/apply/chosen/prepare")
    assert reply.status_code == 409 and "Nothing to make ready" in reply.get_json()["error"] and started == []
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert sorted(desk.calls) == [1, 6] and {item_of(1).state, item_of(6).state} == {APPLIED}


# -- exactly that file ---------------------------------------------------------------------------------

def test_each_form_is_given_exactly_the_file_written_down_checked_by_its_checksum(db, desk):
    cfg, _, tmp_path = db
    execution.add([1, 6])
    runner.prepare_chosen(cfg)
    before = {j: recorded(j) for j in (1, 6)}
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert len(Browser.forms) == 2 and sorted(desk.calls) == [1, 6]             # nothing made again
    audit = [json.loads(line) for line in runner.AUDIT_LOG.read_text().splitlines()]
    for form in Browser.forms:
        job_id = next(j for j in (1, 6) if Path(form.files["input#resume"]).name == Path(before[j]["resume"]["path"]).name)
        uploaded, letter = Path(form.files["input#resume"]), Path(form.files["input#cover_letter"])
        assert sha(uploaded) == before[job_id]["resume"]["sha256"] and sha(letter) == before[job_id]["cover_letter"]["sha256"]
        # Given from the attempt's own folder, never the one it was made ready in.
        assert uploaded.parent.name == "send" and uploaded.parent != Path(before[job_id]["resume"]["path"]).parent
        assert (uploaded.parent.parent / "resume.pdf").read_bytes() == uploaded.read_bytes()   # kept beside the run
        entry = next(e for e in audit if e["job_id"] == job_id)
        assert entry["resume"] == str(uploaded) and entry["cover_letter"] == str(letter)
        assert item_of(job_id).state == APPLIED
        assert any(s.startswith("Checked before the form opens: " + uploaded.name) for s in steps(job_id))


def test_a_document_changed_after_it_was_made_ready_is_never_sent_and_retry_makes_it_again(db, desk):
    cfg, _, _ = db
    execution.add([1])
    runner.prepare_chosen(cfg)
    made = Path(recorded(1)["resume"]["path"])
    made.write_bytes(made.read_bytes() + b" -- changed by hand")
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert Browser.forms == [] and item_of(1).state == FAILED                    # no form opened at all
    assert item_of(1).outcome == (f"Not sent: the resume made ready for it ({made.name}) has changed or is gone since "
                                  f"it was made ready on {datetime.now(timezone.utc).date()}, so it was not sent. "
                                  "Retry makes its documents ready again.")
    with get_session() as s:
        assert {a.state for a in s.query(ApplyArtifact)} == {"stale"}           # never used again
    execution.retry(item_of(1).id)
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert desk.calls == [1, 1] and item_of(1).state == APPLIED
    assert sha(Browser.forms[0].files["input#resume"]) == recorded(1)["resume"]["sha256"] == sha(desk.own(1, "resume"))


@pytest.mark.parametrize("change", ["tailored again", "letter edited", "resume gone"])
def test_the_jobs_own_document_changed_since_means_the_one_made_ready_is_not_the_one_meant(db, desk, change):
    cfg, _, _ = db
    execution.add([1])
    runner.prepare_chosen(cfg)
    if change == "tailored again":
        desk.write(1, "resume", "%PDF-1.4 tailored again on the job's page")
    elif change == "letter edited":
        desk.write(1, "letter", "%PDF-1.4 the letter as edited on the job's page")
    else:
        desk.own(1, "resume").unlink()
    runner.run(cfg, chosen=True, browser_factory=Browser)
    what, name = ("cover letter", "letter.pdf") if change == "letter edited" else ("resume", "resume.pdf")
    assert Browser.forms == [] and item_of(1).state == FAILED
    assert item_of(1).outcome.startswith(f"Not sent: the {what} made ready for it ({name}, the job's own) has changed")


def test_documents_made_on_the_way_are_written_down_and_checked_just_before_the_form(db, desk, monkeypatch):
    cfg, _, _ = db
    execution.add([1, 6])
    tampered = []
    real_job = execution._job

    def change_one_while_it_waits(job_id):                   # read just after Ready, before the form
        if job_id == 6 and not tampered:
            path = Path(recorded(6)["resume"]["path"])
            path.write_bytes(b"%PDF-1.4 swapped while it waited")
            tampered.append(path)
        return real_job(job_id)
    monkeypatch.setattr(execution, "_job", change_one_while_it_waits)
    runner.run(cfg, chosen=True, browser_factory=Browser)
    run = last_run()
    docs = recorded(1)
    assert (docs["resume"]["run_id"], docs["resume"]["attempt"]) == (run.id, 1)  # this run, this attempt
    assert len(Browser.forms) == 1 and sha(Browser.forms[0].files["input#resume"]) == docs["resume"]["sha256"]
    assert item_of(1).state == APPLIED
    assert item_of(6).state == FAILED and "has changed or is gone since" in item_of(6).outcome


# -- a writer that bills, only with a yes ------------------------------------------------------------

def test_which_writers_bill_per_use(db):
    cfg, _, _ = db
    free = {name for name in llm.PROVIDERS if runner.writer({"resume.provider": name})[2]}
    assert free == {"ollama", "claude-code", "chatgpt", "gemini", "omniroute"}
    assert runner.writer({"resume.provider": "anthropic"}) == ("anthropic", "Anthropic API", False)


def test_a_busy_writer_falls_back_only_to_free_ones_never_to_one_that_bills(monkeypatch):
    tried = []

    def dispatch(provider, system, user, cfg, *, max_tokens, want_json):
        tried.append(provider)
        raise llm.ProviderBusy(f"{provider} is busy")

    def ollama(system, user, cfg=None, *, want_json=True, model=None):
        tried.append(f"ollama:{model}")
        raise llm.ProviderBusy("busy")
    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "_ollama", ollama)
    monkeypatch.setattr(llm, "other_plans", lambda provider, cfg=None: ["chatgpt", "gemini"])
    monkeypatch.setattr(llm, "fallback_models", lambda cfg=None, skip="": ["gemma4:e4b"])
    monkeypatch.setenv("RESUME_PROVIDER", "claude-code")
    with pytest.raises(llm.ProviderBusy):
        llm.complete("system", "user", {})
    assert tried == ["claude-code", "chatgpt", "gemini", "ollama:gemma4:e4b"]
    assert all(llm.PROVIDERS[name.split(":")[0]]["free"] for name in tried)


def test_a_writer_that_bills_writes_only_once_the_person_says_yes(db, desk, client, monkeypatch):
    cfg, _, _ = db
    c, started = client
    monkeypatch.setenv("RESUME_PROVIDER", "anthropic")
    execution.add([1])
    assert runner.prepare_chosen(cfg) == 0 and desk.calls == []
    assert last_run().note.startswith("Not prepared: tailoring and cover letters would use Anthropic API, which bills")
    assert runner.run(cfg, chosen=True, browser_factory=Browser) == [] and desk.calls == [] and Browser.forms == []
    assert last_run().note.startswith("Not started: a resume or cover letter made on the way would use Anthropic API")
    for route, task in (("/apply/chosen/prepare", "prepare-chosen"), ("/apply/chosen/start", "apply-chosen")):
        before = len(started)
        asked = c.post(route)
        assert asked.status_code == 409 and asked.get_json()["paid_writer"] == "Anthropic API"
        assert len(started) == before                                # asked, not started
        assert c.post(route, json={"paid": True}).get_json()["ok"] is True
        assert started[-1] == (task, [*runner_args(task), "--paid-ok"])
    assert runner.prepare_chosen(cfg, paid_ok=True) == 1 and desk.calls == [1]


def test_cover_letters_count_too_a_writer_that_bills_is_asked_for_even_with_tailoring_off(db, desk, monkeypatch):
    cfg, _, _ = db
    monkeypatch.setenv("RESUME_PROVIDER", "openai")
    execution.add([1])
    assert runner.prepare_chosen(cfg, tailor=False) == 0 and desk.calls == []
    assert last_run().note.startswith("Not prepared: tailoring and cover letters would use OpenAI API")


def test_a_resume_set_to_be_made_again_is_never_sent_as_it_was_made_ready(db, desk):
    """A tailored resume made from a master since repaired is never sent as it is (runner.choose_resume,
    17 Oct 2026): one made ready before the flag was set is made again, by the run and by Prepare these."""
    from ml.tailoring import pipeline
    cfg, _, _ = db
    execution.add([1, 6])
    runner.prepare_chosen(cfg)
    first = {j: recorded(j)["resume"]["sha256"] for j in (1, 6)}
    pipeline.mark_redo([6], "made from the master resume before it was repaired")
    desk.write(6, "resume", "%PDF-1.4 tailored again from the repaired master, job 6")
    assert runner.prepare_chosen(cfg) == 1                        # job 6: Prepare these makes it again ...
    # Tailored again, its flag is cleared (as the tailoring does); then job 1's is set, after it was made ready.
    pipeline.REDO.write_text(json.dumps({"1": "made from the master resume before it was repaired"}))
    desk.write(1, "resume", "%PDF-1.4 tailored again from the repaired master")
    runner.run(cfg, chosen=True, browser_factory=Browser)         # ... and job 1 the run makes again
    assert sorted(desk.calls) == [1, 1, 6, 6]
    for job_id in (1, 6):
        given = next(f for f in Browser.forms if f"_{'Globex' if job_id == 1 else 'Massive_Dynamic'}" in f.files["input#resume"])
        assert sha(given.files["input#resume"]) == sha(desk.own(job_id, "resume")) != first[job_id]
        assert item_of(job_id).state == APPLIED
    assert any(s.startswith("Made ready before, but its resume is to be made again first: the one made for it was "
                            "made from the master resume") for s in steps(1))


def runner_args(task):
    from backend.api.app import TASKS
    return TASKS[task]


# -- crashes, and one run at a time ---------------------------------------------------------------------

def test_a_preparation_cut_short_by_a_crash_is_made_ready_again_and_never_half_written(db, desk):
    cfg, _, _ = db
    execution.add([1])
    with get_session() as s:
        run = ApplyRun(state="running", pid=dead_pid(), started_at=datetime.now(timezone.utc).replace(tzinfo=None))
        s.add(run)
        s.flush()
        run_id = run.id
    execution.move(item_of(1).id, VALIDATING, run_id=run_id)
    execution.move(item_of(1).id, PREPARING, run_id=run_id)       # died while tailoring: nothing written yet
    execution.recover()
    assert item_of(1).state == QUEUED and recorded(1) == {}
    assert runner.prepare_chosen(cfg) == 1 and set(recorded(1)) == {"resume", "cover_letter"}


def test_documents_written_down_before_a_crash_are_used_after_it_not_made_again(db, desk):
    cfg, _, _ = db
    execution.add([1])
    with get_session() as s:
        run = ApplyRun(state="running", pid=dead_pid(), started_at=datetime.now(timezone.utc).replace(tzinfo=None))
        s.add(run)
        s.flush()
        run_id = run.id
    item = item_of(1).id
    execution.move(item, VALIDATING, run_id=run_id)
    execution.move(item, PREPARING, run_id=run_id)
    execution.record_documents(item, desk.prepare_documents(cfg, execution._job(1), dana(), "", tailor=True), run_id=run_id)
    execution.move(item, READY, run_id=run_id)                    # died before its form opened
    execution.recover()
    assert item_of(1).state == QUEUED
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert desk.calls == [1] and item_of(1).state == APPLIED
    assert sha(Browser.forms[0].files["input#resume"]) == recorded(1)["resume"]["sha256"]


def test_never_beside_another_run(db, desk, client):
    cfg, _, _ = db
    c, started = client
    execution.add([1])
    execution.start_run(os.getpid())                              # Apply to these, going
    with pytest.raises(execution.Busy):
        runner.prepare_chosen(cfg)
    reply = c.post("/apply/chosen/prepare")
    assert reply.status_code == 409 and "A run is going already" in reply.get_json()["error"] and started == []
    assert desk.calls == [] and recorded(1) == {}


# -- the list, and the database ------------------------------------------------------------------------

def test_the_list_shows_each_jobs_files_by_name_and_checksum_not_where_they_are(db, desk, client):
    cfg, _, _ = db
    c, _ = client
    execution.add([1, 6])
    view = c.get("/apply/chosen").get_json()
    assert view["to_prepare"] == 2 and view["writer"] == {"label": "Claude, through Claude Code (your plan)", "free": True}
    runner.prepare_chosen(cfg)
    item = next(i for i in c.get("/apply/chosen").get_json()["items"] if i["job_id"] == 1)
    names = [(d["kind"], d["name"], d["sha256"]) for d in item["documents"]]
    assert names == [("resume", "Dana_Example_Resume_Globex.pdf", sha(desk.own(1, "resume"))),
                     ("cover_letter", "Dana_Example_Cover_Letter_Globex.pdf", sha(desk.own(1, "letter")))]
    assert not {"path", "origin", "origin_sha256"} & set(item["documents"][0])
    assert execution.item_views([item["id"]], False)[0]["documents"] == item["documents"]   # the stream's view too


def test_a_database_from_before_gains_the_documents_table_and_keeps_every_row(tmp_path, engine_back):
    phase_two = ("CREATE TABLE apply_items (id INTEGER PRIMARY KEY, job_id INTEGER, state VARCHAR(24), "
                 "position INTEGER, attempts INTEGER, run_id INTEGER, note TEXT, action TEXT, outcome TEXT, "
                 "hidden BOOLEAN, added_at DATETIME, updated_at DATETIME, finished_at DATETIME)")
    when = "'2026-10-25 20:00:00.000000'"
    url = old_database(tmp_path / "phase2.db", apply_tables_sql=(
        phase_two, CreateTable(ApplyRun.__table__), CreateTable(ApplyEvent.__table__),
        f"INSERT INTO apply_items (id, job_id, state, position, attempts, hidden, added_at, updated_at) "
        f"VALUES (1, 1, 'queued', 1, 0, 0, {when}, {when})"))
    init_engine(url)
    tables = inspect(session_mod._engine).get_table_names()
    assert "apply_artifacts" in tables
    columns = {c["name"] for c in inspect(session_mod._engine).get_columns("apply_artifacts")}
    assert {"item_id", "job_id", "run_id", "attempt", "kind", "path", "sha256", "origin_path", "origin_sha256",
            "state"} <= columns
    with get_session() as s:
        assert [(i.id, i.state) for i in s.query(ApplyItem)] == [(1, QUEUED)]


# -- ready to go by themselves: quality, not only an unchanged checksum (29 Oct 2026) ------------------

@pytest.mark.parametrize("case,why", [
    ("ready", None),
    ("no run", "its resume has no checked tailoring run to vouch for it"),
    ("checks failed", "its resume did not pass all of its own checks"),
    ("drafting problem", "its tailoring noted a problem (the model left a line half-written)"),
    ("another file", "the resume about to go is not the one its checked tailoring run made"),
    ("a letter", "a cover letter goes with it, and a letter's checks are not kept"),
    ("to be made again", "its resume is set to be made again"),
])
def test_documents_go_by_themselves_only_when_their_own_checks_vouch_for_them(tmp_path, case, why):
    from ml.tailoring import pipeline as tailoring
    resume = tmp_path / "Dana_Example_Resume_Globex.pdf"
    resume.write_bytes(b"%PDF-1.4 the resume made for job 1")
    letter = None
    if case == "ready":
        checked_run(1, resume)
    elif case == "checks failed":
        checked_run(1, resume, ok=False)
    elif case == "drafting problem":
        checked_run(1, resume, note="the model left a line half-written")
    elif case == "another file":
        other = tmp_path / "other.pdf"
        other.write_bytes(b"%PDF-1.4 an earlier resume")
        checked_run(1, other)
    elif case == "a letter":
        checked_run(1, resume)
        letter = tmp_path / "letter.pdf"
        letter.write_bytes(b"%PDF-1.4 a letter")
    elif case == "to be made again":
        checked_run(1, resume)
        tailoring.REDO.write_text(json.dumps({"1": "made from a master since repaired"}))
    assert execution.documents_unready(1, resume, letter) == why


def test_a_run_leaves_submit_to_the_person_when_its_documents_are_not_vouched_for(db, desk, monkeypatch):
    cfg, _, _ = db
    went = []

    def fill_form(cfg, page, job, applicant, head, ready, *, auto, wait_s):
        went.append((job.id, auto))
        return runner.Outcome(job.id, job.company, job.title, "skipped")
    monkeypatch.setattr(runner, "fill_form", fill_form)
    monkeypatch.setattr(runner, "load", lambda path: dana(stop_before_submit=False))
    from backend.apply import prepare
    monkeypatch.setattr(prepare, "open_questions", lambda job, applicant: [])
    desk.letters = False                                       # no letter, but no checked tailoring run either
    execution.add([1])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert went == [(1, False)]
    with get_session() as s:
        said = [e.message for e in s.query(ApplyEvent).filter(ApplyEvent.job_id == 1)]
    assert "You press Submit on this one: its resume has no checked tailoring run to vouch for it" in said
