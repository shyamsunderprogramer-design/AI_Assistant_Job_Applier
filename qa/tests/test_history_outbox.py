"""Application-history durability: isolated SQLite, real process deaths, no external services."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from backend.apply import execution, history_outbox, runner
from data_engineering.db.migrate import ensure_history_outbox
from data_engineering.db.models import ApplyArtifact, ApplyHistoryOutbox, Job
from data_engineering.db.session import get_session
from qa.tests.test_execution_queue import db, item_of  # noqa: F401

WHEN = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
ENTRY = {"company": "Fictional Co", "title": "Engineer", "how": "you", "resume": "/fictional/resume.pdf",
         "cover_letter": "/fictional/letter.pdf", "queue": {"item_id": 321, "run_id": 654, "attempt": 7,
         "documents": [{"kind": "resume", "made_ready": "/fictional/resume.pdf", "sha256": "a" * 64,
                        "sent": "/fictional/attempt/resume.pdf"}]}}


def rows():
    with get_session() as session:
        return [(r.event_id, json.loads(r.payload), r.exported_at) for r in
                session.query(ApplyHistoryOutbox).order_by(ApplyHistoryOutbox.created_at)]


def lines():
    return [json.loads(line) for line in runner.AUDIT_LOG.read_text().splitlines()] if runner.AUDIT_LOG.exists() else []


def pending(monkeypatch, job_id=1):
    with monkeypatch.context() as patch:
        patch.setattr(runner, "write_audit", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            runner.record_application(job_id, ENTRY, at=WHEN)


def test_pending_payload_is_immutable_and_recovery_preserves_all_links(db, monkeypatch):
    with get_session() as s:
        s.add(ApplyArtifact(item_id=321, job_id=1, run_id=654, attempt=7, kind="resume",
                            path="/fictional/resume.pdf", sha256="a" * 64, state="ready"))
    pending(monkeypatch)
    event_id, payload, exported = rows()[0]
    assert exported is None and payload["queue"] == ENTRY["queue"]
    assert payload["at"] == WHEN.isoformat() and payload["job_id"] == 1
    with get_session() as session:
        assert session.query(ApplyArtifact).one().state == "sent"
        session.get(Job, 1).title = "Changed after application"
    for _ in range(3):
        execution.recover()
    assert lines() == [payload]
    assert rows()[0][0] == event_id and rows()[0][2] is not None
    assert runner.record_application(1, {"how": "different retry"}) is False
    assert lines() == [payload]


@pytest.mark.parametrize("chosen", [False, True])
def test_outbox_insert_failure_rolls_back_the_application_state(db, monkeypatch, chosen):
    if chosen:
        execution.add([1])
        item = item_of(1)
        for state in (execution.VALIDATING, execution.PREPARING, execution.READY, execution.RUNNING,
                      execution.PENDING):
            execution.move(item.id, state)
    original = history_outbox.enqueue

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("outbox commit failed")
    monkeypatch.setattr(history_outbox, "enqueue", fail)
    with pytest.raises(OSError, match="outbox commit failed"):
        if chosen:
            execution.confirm(item.id, went=True)
        else:
            runner.record_application(1, ENTRY)
    with get_session() as session:
        assert session.get(Job, 1).status == "Not Applied"
        assert session.get(Job, 1).applied_at is None
    assert rows() == [] and lines() == []
    if chosen:
        assert item_of(1).state == execution.PENDING


def test_existing_history_bytes_and_old_applications_are_never_backfilled(db, monkeypatch):
    original = b'{"job_id":2,"at":"2001-01-01","how":"legacy"}\n'
    runner.AUDIT_LOG.write_bytes(original)
    with get_session() as s:
        s.get(Job, 2).status = "Applied"
        s.get(Job, 2).applied_at = WHEN.replace(tzinfo=None)
    assert runner.record_application(2, {"how": "must not backfill"}) is False
    execution.recover()
    assert rows() == [] and runner.AUDIT_LOG.read_bytes() == original
    pending(monkeypatch)
    execution.recover()
    assert runner.AUDIT_LOG.read_bytes().startswith(original)
    assert [row["job_id"] for row in lines()] == [2, 1]


def test_narrow_migration_adds_only_empty_outbox_and_is_repeatable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("create table jobs (id integer primary key, status text, applied_at text)")
        conn.exec_driver_sql("insert into jobs values (7, 'Applied', '2001-01-01')")
    ensure_history_outbox(engine)
    ensure_history_outbox(engine)
    assert set(inspect(engine).get_table_names()) == {"jobs", "apply_history_outbox"}
    with engine.connect() as conn:
        assert conn.exec_driver_sql("select * from jobs").all() == [(7, "Applied", "2001-01-01")]
        assert conn.exec_driver_sql("select count(*) from apply_history_outbox").scalar() == 0
    engine.dispose()


CHILD = r'''
import json, os, socket, sys
from pathlib import Path
def refuse(*a, **k): raise RuntimeError("no network in history tests")
socket.socket.connect = socket.socket.connect_ex = socket.getaddrinfo = refuse
from backend.apply import execution, history_outbox, runner
from data_engineering.db.session import init_engine, get_session
from data_engineering.db.models import ApplyHistoryOutbox
from datetime import datetime, timezone
url, audit, operation, point, mode, item, entry = sys.argv[1:]
runner.AUDIT_LOG = Path(audit)
init_engine(url)
entry = json.loads(entry)
def crash(*a, **kw): os._exit(73)
if point == "before_commit":
    original = history_outbox.enqueue
    def enqueue(*a, **kw):
        original(*a, **kw)
        crash()
    history_outbox.enqueue = enqueue
elif point == "after_commit": runner.write_audit = crash
elif point == "before_publish": history_outbox.os.replace = crash
elif point == "after_publish": history_outbox._acknowledge = crash
elif point == "during_write":
    original = history_outbox.os.fdopen
    class Partial:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): self.handle.close()
        def write(self, data):
            if data:
                self.handle.write(data[:max(1, len(data)//2)])
                self.handle.flush()
                os.fsync(self.handle.fileno())
                crash()
            return 0
    history_outbox.os.fdopen = lambda *a, **kw: Partial(original(*a, **kw))
elif point == "during_ack":
    def acknowledge(event_id):
        with get_session() as session:
            session.query(ApplyHistoryOutbox).filter_by(event_id=event_id).update({"exported_at": datetime.now()})
            session.flush()
            crash()
    history_outbox._acknowledge = acknowledge
if operation == "recover": execution.recover()
elif mode == "chosen":
    execution.answer(int(item), 1, True, entry, expect=(execution.PENDING,), outcome="Applied in a fictional test")
else:
    runner.record_application(1, entry, at=datetime(2026,1,2,3,4,5,123456,tzinfo=timezone.utc))
'''


def spawn(db, operation, point="none", mode="runner", item=0):
    _, url, folder = db
    script = folder / "outbox-child.py"
    if not script.exists():
        script.write_text(CHILD)
    # No credential environment, no real config/profile, no external commands or forms.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(Path.cwd())}
    return subprocess.Popen([sys.executable, str(script), url, str(runner.AUDIT_LOG), operation,
                             point, mode, str(item), json.dumps(ENTRY)], env=env, cwd=folder,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def finish(process, expected=0):
    stdout, stderr = process.communicate(timeout=45)
    assert process.returncode == expected, (stdout, stderr)


@pytest.mark.parametrize("mode", ["runner", "chosen"])
@pytest.mark.parametrize("point", ["before_commit", "after_commit", "during_write", "before_publish",
                                    "after_publish", "during_ack"])
def test_process_death_at_each_boundary(db, mode, point):
    item = 0
    if mode == "chosen":
        execution.add([1])
        item = item_of(1).id
        for state in (execution.VALIDATING, execution.PREPARING, execution.READY, execution.RUNNING,
                      execution.PENDING):
            execution.move(item, state)
    finish(spawn(db, "record", point, mode, item), expected=73)
    with get_session() as s:
        job = s.get(Job, 1)
        assert job.status == ("Not Applied" if point == "before_commit" else "Applied")
        original_time = job.applied_at
    if point == "before_commit":
        assert rows() == [] and lines() == []
        if mode == "chosen": assert item_of(1).state == execution.PENDING
        return
    event_id, payload, exported = rows()[0]
    assert exported is None
    # Recovery itself dies after publication: the next startup must deduplicate that line.
    finish(spawn(db, "recover", "after_publish"), expected=73)
    finish(spawn(db, "recover"))
    finish(spawn(db, "recover"))
    assert lines() == [payload] and rows()[0][0] == event_id and rows()[0][2] is not None
    assert payload["queue"] == ENTRY["queue"] and payload["resume"] == ENTRY["resume"]
    with get_session() as s:
        assert s.get(Job, 1).applied_at == original_time


def test_concurrent_recorders_and_recovery_export_each_event_once(db, monkeypatch):
    processes = [spawn(db, "record") for _ in range(6)]
    for p in processes: finish(p)
    assert len(rows()) == len(lines()) == 1
    pending(monkeypatch, 2)
    pending(monkeypatch, 3)
    processes = [spawn(db, "recover") for _ in range(6)]
    for p in processes: finish(p)
    assert len(rows()) == len(lines()) == 3
    assert {row["job_id"] for row in lines()} == {1, 2, 3}
    assert len({row["history_event_id"] for row in lines()}) == 3
    assert all(exported is not None for _, _, exported in rows())
