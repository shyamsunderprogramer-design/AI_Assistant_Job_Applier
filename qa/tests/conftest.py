"""Shared test setup.

Evidence from the form filler and the application log are written to real
folders by default. Tests must never add to them: before this fixture existed
the filler tests had left 93 fake "runs" among the real ones.
"""

import os
from pathlib import Path

import pytest

# -- nothing a test does reaches past this Mac (24 Oct 2026) ----------------------------------
# A route test once loaded the real config and .env into the run, and the provider tests after
# it launched the real `claude` command. So no test may reach another host, the local model
# servers or the running app, start an AI command-line tool or a browser, or leave environment
# variables behind for the next test.

BLOCKED_COMMANDS = {"claude", "gemini", "codex", "chatgpt", "ollama", "omniroute", "gh", "open",
                    "osascript", "curl", "wget"}
BLOCKED_LOCAL_PORTS = {11434, 20128, 8770}          # Ollama, OmniRoute, the app itself


def _loopback(host) -> bool:
    host = host.decode() if isinstance(host, bytes) else str(host)
    return host in ("localhost", "::1", "") or host.startswith("127.")


# -- the person's own records are never touched (25 Oct 2026) -----------------------------------
# A test that called monkeypatch.undo() undid the redirects below with its own patch, and its "yes"
# went into the real application log (found, and taken out again, the same evening). Whatever a
# test does, these files are as they were before it ran, or the test fails and names them.
OWN_RECORDS = ("data/applications.jsonl", "data/apply/unconfirmed.json", "data/apply/chosen.json",
               "data/apply/prepared.json", "data/watchlist.json", "data/written_answers.json")


REAL_ROOT = Path(__file__).resolve().parents[2]     # fixed here: some tests point PROJECT_ROOT elsewhere


def _stamp() -> dict:
    stamps = {}
    for name in OWN_RECORDS:
        try:
            info = os.stat(REAL_ROOT / name)
            stamps[name] = (info.st_size, info.st_mtime_ns)
        except OSError:
            stamps[name] = None
    return stamps


@pytest.fixture(autouse=True)
def _own_records_untouched():
    before = _stamp()
    yield
    changed = [name for name, stamp in _stamp().items() if stamp != before[name]]
    assert not changed, f"this test changed the person's own records: {changed}"


# -- and never the person's own database (25 Oct 2026) ------------------------------------------
# A test that found no database set up opened the configured one -- the person's data/jobs.db --
# and the app's start-up check added a new column to it. Whatever a test does, it opens a database
# of its own: asked for the real one, init_engine refuses, wherever it was imported.
@pytest.fixture(autouse=True)
def _never_the_real_database(monkeypatch):
    import sys

    from data_engineering.db import session
    original, real = session.init_engine, (REAL_ROOT / "data" / "jobs.db").resolve()

    def init_engine(url, *args, **kwargs):
        if url.startswith("sqlite:///") and ":memory:" not in url:
            path = Path(url[len("sqlite:///"):])
            if (path if path.is_absolute() else REAL_ROOT / path).resolve() == real:
                raise PermissionError("a test may not open the person's own database (data/jobs.db)")
        return original(url, *args, **kwargs)

    for module in list(sys.modules.values()):
        if getattr(module, "init_engine", None) is original:
            monkeypatch.setattr(module, "init_engine", init_engine)


# -- nor read it past the app's engine (26 Oct 2026) -------------------------------------------
# The salary rule reads a job's pay straight from data/jobs.db (backend/apply/salary.job_by_id), and
# seven of the questions tests read the person's database that way, by made-up job ids: read-only,
# and no result depended on it (all passed against an empty stand-in), but theirs all the same.
# Opening any of the person's databases directly is refused now, and the salary rule reads the
# test's own database.
@pytest.fixture(autouse=True)
def _never_the_real_database_directly(monkeypatch):
    import sqlite3
    from types import SimpleNamespace
    from urllib.parse import unquote

    from backend.apply import salary
    real, connect = (REAL_ROOT / "data").resolve(), sqlite3.connect

    def guarded(database, *args, **kwargs):
        text = str(database)
        path = Path(unquote(text[5:].split("?")[0]) if text.startswith("file:") else text)
        if text not in ("", ":memory:") and path.suffix == ".db" and path.resolve().parent == real:
            raise PermissionError(f"a test may not open the person's own database (data/{path.name})")
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", guarded)

    def job_by_id(job_id):
        from data_engineering.db.models import Job
        from data_engineering.db.session import get_session
        with get_session() as session:
            job = session.get(Job, job_id)
            return None if job is None else SimpleNamespace(
                title=job.title, salary_min=job.salary_min, salary_max=job.salary_max, salary_period=job.salary_period,
                salary_currency=job.salary_currency, company=job.company, id=job_id)
    monkeypatch.setattr(salary, "job_by_id", job_by_id)


@pytest.fixture(autouse=True)
def _nothing_leaves_this_mac(monkeypatch):
    import socket
    import subprocess

    environment = dict(os.environ)

    def allowed(address):
        if isinstance(address, tuple) and (not _loopback(address[0]) or address[1] in BLOCKED_LOCAL_PORTS):
            raise ConnectionRefusedError(f"a test may not connect to {address[0]}:{address[1]}")

    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: (allowed(address), connect(self, address))[1])
    monkeypatch.setattr(socket.socket, "connect_ex",
                        lambda self, address: (allowed(address), connect_ex(self, address))[1])

    lookup = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _loopback(host):
            raise socket.gaierror(f"a test may not look up {host}")
        return lookup(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    # The one live test there is (Word lays out a .docx) runs only when asked for.
    blocked = BLOCKED_COMMANDS - ({"osascript"} if environment.get("JOB_APPLIER_LIVE_WORD") == "1" else set())

    class Popen(subprocess.Popen):
        def __init__(self, args, *rest, **kwargs):
            first = args if isinstance(args, (str, bytes, os.PathLike)) else (args[0] if args else "")
            name = os.path.basename(str(first).split()[0]) if str(first).strip() else ""
            if name in blocked:
                raise PermissionError(f"a test may not run {name}")
            super().__init__(args, *rest, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", Popen)
    yield
    os.environ.clear()
    os.environ.update(environment)


@pytest.fixture(autouse=True)
def _keep_apply_output_out_of_the_repo(tmp_path, monkeypatch):
    from backend.apply import greenhouse, runner

    monkeypatch.setattr(greenhouse, "RUNS_DIR", tmp_path / "apply-runs")
    monkeypatch.setattr(runner, "AUDIT_LOG", tmp_path / "applications.jsonl")


@pytest.fixture(autouse=True)
def _no_real_applicant_in_resumes(monkeypatch):
    """Resume tests must not pick up the contact details of whoever runs them."""
    from ml.resume import writer

    monkeypatch.setattr(writer, "applicant_contact_line", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_answer_bank(tmp_path, monkeypatch):
    """The person's learned answers must not leak into, or be written by, tests."""
    from backend.apply import answers

    monkeypatch.setattr(answers, "BANK_PATH", tmp_path / "answers.yaml")


@pytest.fixture(autouse=True)
def _no_real_chat_plans(monkeypatch):
    """Model fallbacks never ask the real Claude/ChatGPT tools, and "busy" is not
    remembered from one test into the next."""
    from ml.resume import llm
    monkeypatch.setattr(llm, "_BUSY_UNTIL", {})
    monkeypatch.setattr(llm, "other_plans", lambda provider, cfg=None: [])


@pytest.fixture(autouse=True)
def _no_real_general_answers(monkeypatch, tmp_path):
    """General answers and resume employers come from the test, never the person's files."""
    from backend.apply import general
    monkeypatch.setattr(general, "STORE", tmp_path / "general_answers.yaml")
    monkeypatch.setattr(general, "past_employers", lambda: ())


@pytest.fixture(autouse=True)
def _no_real_redo_list(monkeypatch, tmp_path):
    """The person's list of tailored resumes to be made again is never read or changed by tests: a run
    looks at it before sending a resume made ready earlier (26 Oct 2026)."""
    from ml.tailoring import pipeline
    monkeypatch.setattr(pipeline, "REDO", tmp_path / "redo.json")
    # Nor its tailoring runs and each job's best: a run reads their checks before sending by itself
    # (execution.documents_unready, 29 Oct 2026). Tests that need runs make their own.
    monkeypatch.setattr(pipeline, "RUNS", tmp_path / "tailoring-runs")
    monkeypatch.setattr(pipeline, "JOBS", tmp_path / "tailoring-jobs")


@pytest.fixture(autouse=True)
def _no_real_written_answers(monkeypatch, tmp_path):
    """The person's written answers are never drafted from, or added to, by tests."""
    from backend.apply import written
    monkeypatch.setattr(written, "PATH", tmp_path / "written_answers.json")
