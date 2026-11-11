"""Track A1: one preparation per job at a time, and documents that appear whole or not at all
(ml/tailoring/coordination.py). The claim: taken once, given up when released, expired or its owner is gone,
never when it cannot be read; a worker that lost it publishes nothing. The publication: a DOCX and a PDF that
become visible together, only after both are in place and checked, never written again -- with real processes
competing for one job and real processes killed at each step. A database file of the test's own."""

import multiprocessing as mp
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from ml.tailoring import coordination as co
from data_engineering.db import session as session_mod
from data_engineering.db.session import init_engine

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def db(tmp_path, monkeypatch):
    before = (session_mod._engine, session_mod._SessionFactory)
    url = f"sqlite:///{tmp_path / 'jobs.db'}"
    init_engine(url)
    monkeypatch.setattr(co, "PUBLISHED", tmp_path / "published")
    co.ensure_tables()
    yield tmp_path
    session_mod._engine, session_mod._SessionFactory = before


def made(tmp_path, tag):
    folder = tmp_path / f"run-{tag}"
    folder.mkdir(exist_ok=True)
    (folder / "tailored-resume.docx").write_bytes(f"DOCX {tag} ".encode() * 200)
    (folder / "tailored-resume.pdf").write_bytes(f"%PDF {tag} ".encode() * 200)
    return folder / "tailored-resume.docx", folder / "tailored-resume.pdf"


def rows(sql, **kw):
    with session_mod._engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), kw).mappings()]


def dead_pid():
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    return int(done.stdout)


# -- the claim ------------------------------------------------------------------------------------------

def test_one_claim_per_job_and_its_token_goes_up_each_time_it_changes_hands(db):
    first = co.acquire(7, "Prepare these")
    assert first.token == 1
    with pytest.raises(co.LeaseBusy):
        co.acquire(7, "Prepare now")                     # held by this live process
    assert co.acquire(8, "Prepare now").token == 1       # another job: its own claim
    co.release(first)
    assert co.acquire(7, "Prepare now").token == 2


def test_an_expired_claim_is_taken_over_and_the_old_worker_is_fenced_out(db):
    old = co.acquire(7, "a run that hung")
    later = co._now() + timedelta(seconds=co.LEASE_TTL_S + 5)
    new = co.acquire(7, "Prepare now", now=later)
    assert new.token == old.token + 1
    with pytest.raises(co.LeaseLost):
        co.heartbeat(old)
    docx, pdf = made(db, "old")
    with pytest.raises(co.LeaseLost):
        co.publish(old, docx=docx, pdf=pdf, source="tailored", repeat_key="k", run_id="r-old")
    assert rows("SELECT * FROM document_publications") == []          # nothing reserved, nothing written
    assert not (db / "published" / "7").exists()


def test_a_claim_whose_owner_is_gone_is_taken_back_at_once(db):
    with session_mod._engine.begin() as c:
        c.execute(text("INSERT INTO preparation_leases VALUES (7, 4, 'crashed', :h, :p, '', :t, :t, NULL)"),
                  {"h": socket.gethostname(), "p": dead_pid(), "t": co._now()})
    assert co.acquire(7, "Prepare now").token == 5


def test_a_reused_process_number_is_not_the_owner(db):
    me = co.identity()
    with session_mod._engine.begin() as c:
        c.execute(text("INSERT INTO preparation_leases VALUES (7, 2, 'old', :h, :p, 'Mon Jan  1 00:00:00 2001', :t, :t, NULL)"),
                  {"h": me["host"], "p": me["pid"], "t": co._now()})
    assert co.acquire(7, "Prepare now").token == 3


def test_a_claim_held_on_another_machine_is_waited_out_never_guessed(db):
    with session_mod._engine.begin() as c:
        c.execute(text("INSERT INTO preparation_leases VALUES (7, 1, 'elsewhere', 'other-mac', 12345, '', :t, :t, NULL)"),
                  {"t": co._now()})
    with pytest.raises(co.LeaseBusy):
        co.acquire(7, "Prepare now")
    assert co.acquire(7, "Prepare now", now=co._now() + timedelta(seconds=co.LEASE_TTL_S + 1)).token == 2


@pytest.mark.parametrize("damage", [
    "UPDATE preparation_leases SET token = 'two'",
    "UPDATE preparation_leases SET token = 0",
    "UPDATE preparation_leases SET token = -3",
    "UPDATE preparation_leases SET owner_pid = 'x'",
    "UPDATE preparation_leases SET heartbeat_at = 'not a time'",
    "UPDATE preparation_leases SET heartbeat_at = ''",
    # the table itself damaged: its columns gone (the NOT NULL columns refuse a missing value outright)
    "ALTER TABLE preparation_leases RENAME TO broken; CREATE TABLE preparation_leases (job_id INTEGER PRIMARY KEY)",
])
def test_a_damaged_claim_is_treated_as_held_never_as_free(db, damage):
    lease = co.acquire(7, "Prepare these")
    co.release(lease)                                    # released: it WOULD be free if it could be read
    raw = sqlite3.connect(db / "jobs.db")
    raw.executescript(damage)
    raw.close()
    before = rows("SELECT * FROM preparation_leases")
    with pytest.raises(co.LeaseUnreadable):
        co.acquire(7, "Prepare now")
    assert rows("SELECT * FROM preparation_leases") == before        # left as it was: nothing took it over


def test_a_database_that_stays_locked_fails_closed(db, monkeypatch):
    path = db / "jobs.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 0.05})
    monkeypatch.setattr(session_mod, "_engine", engine)
    monkeypatch.setattr(co, "BUSY_TRIES", 2)
    blocker = sqlite3.connect(path, timeout=0.05, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(co.LeaseUnreadable):
            co.acquire(7, "Prepare now")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert rows("SELECT * FROM preparation_leases") == []             # nothing was taken while it was unreadable


def test_threads_racing_for_one_job_get_one_claim(db):
    for _ in range(20):
        job = 100 + _
        won, gate = [], threading.Barrier(6)

        def grab():
            gate.wait()
            try:
                won.append(co.acquire(job, "race").token)
            except co.LeaseBusy:
                pass
        threads = [threading.Thread(target=grab) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert won == [1]


def _child_acquire(url, job, start, out):
    sys.path.insert(0, HERE)
    from data_engineering.db.session import init_engine as init
    from ml.tailoring import coordination as c
    init(url)
    while time.time() < start:
        time.sleep(0.001)
    try:
        lease = c.acquire(job, f"process {os.getpid()}")
        out.put(("won", lease.token))
        time.sleep(1.0)                                  # hold it while the others try
        c.release(lease)
    except c.LeaseBusy:
        out.put(("busy", None))
    except Exception as exc:                             # noqa: BLE001 -- reported to the test
        out.put(("error", repr(exc)))


def test_processes_racing_for_one_job_get_one_claim(db):
    url = f"sqlite:///{db / 'jobs.db'}"
    ctx = mp.get_context("spawn")
    for round_ in range(5):
        out = ctx.Queue()
        start = time.time() + 4
        procs = [ctx.Process(target=_child_acquire, args=(url, 500 + round_, start, out)) for _ in range(4)]
        [p.start() for p in procs]
        [p.join(60) for p in procs]
        results = [out.get(timeout=5) for _ in procs]
        assert sorted(r[0] for r in results) == ["busy", "busy", "busy", "won"], results


# -- publication ----------------------------------------------------------------------------------------

def test_a_pair_is_published_whole_read_only_and_never_rewritten(db):
    lease = co.acquire(7, "Prepare now")
    docx, pdf = made(db, "one")
    p = co.publish(lease, docx=docx, pdf=pdf, source="tailored", repeat_key="k1", run_id="r1")
    now = co.current(7)
    assert (now.version, now.docx_sha256, now.pdf_sha256) == (1, co.sha256(docx), co.sha256(pdf))
    assert now.docx.read_bytes() == docx.read_bytes() and now.pdf.read_bytes() == pdf.read_bytes()
    assert not os.access(now.docx, os.W_OK) and not os.access(now.pdf, os.W_OK)
    docx2, pdf2 = made(db, "two")
    co.publish(lease, docx=docx2, pdf=pdf2, source="tailored", repeat_key="k2", run_id="r2")
    assert co.current(7).version == 2 and p.docx.read_bytes() == docx.read_bytes()   # version 1 untouched
    assert co.withdraw(7, 2, "rolled back") and co.current(7).version == 1


def test_a_file_changed_after_publication_is_not_offered(db):
    lease = co.acquire(7, "Prepare now")
    docx, pdf = made(db, "one")
    p = co.publish(lease, docx=docx, pdf=pdf, source="tailored", repeat_key="k1", run_id="r1")
    os.chmod(p.pdf, 0o644)
    p.pdf.write_bytes(b"%PDF tampered")
    assert co.current(7) is None                         # never the tampered pair, never an older one instead


def test_readers_never_see_a_mixed_or_partial_pair(db):
    lease = co.acquire(7, "Prepare now")
    seen, stop, versions = [], threading.Event(), {}

    def read():
        while not stop.is_set():
            p = co.current(7, verify=False)
            if p is not None:
                seen.append((p.version, co.sha256(p.docx), co.sha256(p.pdf)))
    reader = threading.Thread(target=read)
    reader.start()
    for n in range(1, 16):
        docx, pdf = made(db, f"v{n}")
        p = co.publish(lease, docx=docx, pdf=pdf, source="tailored", repeat_key=f"k{n}", run_id=f"r{n}")
        versions[p.version] = (p.docx_sha256, p.pdf_sha256)
    stop.set()
    reader.join()
    assert seen and all(versions[v] == (d, f) for v, d, f in seen)   # each read: one version's own two files


CRASH_CHILD = r"""
import os, sys
sys.path.insert(0, {here!r})
from data_engineering.db.session import init_engine
from ml.tailoring import coordination as co
init_engine({url!r})
co.PUBLISHED = __import__("pathlib").Path({published!r})
lease = co.acquire(7, "a run that dies")
def crash(stage):
    if stage == {stage!r}:
        os._exit(9)                                      # the process dies here: nothing runs after it
co.publish(lease, docx={docx!r}, pdf={pdf!r}, source="tailored", repeat_key="new", run_id="dying", crash=crash)
"""


@pytest.mark.parametrize("stage", ["reserved", "docx", "pdf", "before_visible"])
def test_a_process_killed_at_each_publishing_step_leaves_the_old_pair_and_recovers(db, stage):
    first = co.acquire(7, "Prepare now")
    docx, pdf = made(db, "old")
    co.publish(first, docx=docx, pdf=pdf, source="tailored", repeat_key="old", run_id="r-old")
    co.release(first)
    new_docx, new_pdf = made(db, "new")
    script = CRASH_CHILD.format(here=HERE, url=f"sqlite:///{db / 'jobs.db'}", published=str(db / "published"),
                                stage=stage, docx=str(new_docx), pdf=str(new_pdf))
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 9, done.stderr
    now = co.current(7)
    assert now.version == 1 and now.run_id == "r-old"                 # the reader still sees the old pair, whole
    assert co.recover()["failed"] == 1
    assert [r["state"] for r in rows("SELECT state FROM document_publications ORDER BY version")] == ["published", "failed"]
    retry = co.acquire(7, "Retry")                                    # its dead owner's claim is taken back
    p = co.publish(retry, docx=new_docx, pdf=new_pdf, source="tailored", repeat_key="new", run_id="r-new")
    assert co.current(7).version == p.version == 3 and co.sha256(co.current(7).pdf) == co.sha256(new_pdf)


def test_a_worker_that_loses_its_claim_while_copying_publishes_nothing(db):
    old = co.acquire(7, "slow")
    docx, pdf = made(db, "slow")

    def taken_over(stage):
        if stage == "pdf":                                            # someone else takes the job meanwhile
            co.acquire(7, "Prepare now", now=co._now() + timedelta(seconds=co.LEASE_TTL_S + 5))
    with pytest.raises(co.LeaseLost):
        co.publish(old, docx=docx, pdf=pdf, source="tailored", repeat_key="k", run_id="slow", crash=taken_over)
    assert co.current(7) is None
    assert [r["state"] for r in rows("SELECT state FROM document_publications")] == ["failed"]


def test_recover_leaves_a_pending_version_of_a_live_claim_alone(db):
    lease = co.acquire(7, "working")
    with session_mod._engine.begin() as c:
        c.execute(text("INSERT INTO document_publications (job_id, version, state, source, repeat_key, token, docx_path, "
                       "pdf_path, docx_sha256, pdf_sha256, created_at, note) VALUES (7, 1, 'pending', 'tailored', 'k', :k, "
                       "'a', 'b', 'x', 'y', :t, '')"), {"k": lease.token, "t": co._now()})
    assert co.recover()["failed"] == 0                                # its worker is alive and still holds it
    co.release(lease)
    assert co.recover()["failed"] == 1


def test_a_record_pointing_outside_the_published_folder_is_not_followed(db):
    lease = co.acquire(7, "Prepare now")
    docx, pdf = made(db, "one")
    co.publish(lease, docx=docx, pdf=pdf, source="tailored", repeat_key="k1", run_id="r1")
    elsewhere = db / "secret.txt"
    elsewhere.write_bytes(pdf.read_bytes())                         # the same bytes, somewhere else
    with session_mod._engine.begin() as c:
        c.execute(text("UPDATE document_publications SET pdf_path = :p"), {"p": str(elsewhere)})
    assert co.current(7) is None
