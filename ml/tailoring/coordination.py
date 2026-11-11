"""One preparation per job at a time, and documents that appear whole or not at all (Track A1, Nov 2026).

Two things a job's resume preparation needs that a folder of files cannot give it:

  * A CLAIM (data_engineering.db.models.PreparationLease), one row per job. Whoever holds it is the
    only one preparing that job's resume. It names its owner -- this machine, a process id and that
    process's own start time, so a process id reused by another program is not mistaken for the
    owner -- and is kept alive by a heartbeat. It is given up when released, when its heartbeat is
    older than LEASE_TTL_S, or at once when its owner's process is gone. Each change of hands adds
    one to its `token`; nothing a worker does counts unless its token is still the row's (fencing).
    A claim that cannot be read -- the row damaged, or the database not answering -- is treated as
    HELD by someone else, never as free: preparing a job twice is the one thing this must prevent.

  * A PUBLICATION (DocumentPublication), one row per version of a job's DOCX and PDF. The two files
    are copied into a folder of their own (data/tailoring/published/<job>/v<version>-<token>/), each
    written under a temporary name, flushed to disk and renamed into place, then made read-only and
    checked against the fingerprints recorded first. Only then, in one update -- and only while the
    claim is still the writer's -- does the row become `published`. A reader asks for the newest
    published version (`current`): it never sees one file of a pair without the other, a half-written
    file, or anything a worker wrote after losing its claim. Nothing published is ever written again.

`recover` settles what a crash leaves: a version left `pending` by a worker that no longer holds the
claim becomes `failed` and is never shown. The files of a failed version stay where they are.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.config.loader import PROJECT_ROOT

LEASE_TTL_S = 15 * 60          # a claim with no heartbeat this long is given up
HEARTBEAT_S = 30
BUSY_TRIES = 12                # "database is locked": asked again this many times, then fail closed
PUBLISHED = PROJECT_ROOT / "data" / "tailoring" / "published"
PENDING, LIVE, FAILED, WITHDRAWN = "pending", "published", "failed", "withdrawn"


class CoordinationError(RuntimeError):
    """Why a preparation cannot go on, in words the person can act on."""


class LeaseBusy(CoordinationError):
    def __init__(self, purpose: str, since: str):
        super().__init__(f"Its resume is being made already ({purpose or 'another preparation'}, since {since}). "
                         "Wait for it to finish, then press this again.")
        self.purpose = purpose


class LeaseUnreadable(CoordinationError):
    """The claim could not be read or written: treated as held, so nothing is prepared twice."""


class LeaseLost(CoordinationError):
    """Another worker took the claim over: what this one made is never published."""


class PublishError(CoordinationError):
    pass


@dataclass
class Lease:
    job_id: int
    token: int
    purpose: str
    lost: bool = False
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)


@dataclass
class Publication:
    id: int
    job_id: int
    version: int
    source: str
    repeat_key: str
    run_id: str | None
    docx: Path
    pdf: Path
    docx_sha256: str
    pdf_sha256: str
    published_at: datetime | None
    note: str = ""


# -- who is asking ------------------------------------------------------------------------------------

def _process_start(pid: int) -> str:
    """The process's own start time as the system reports it, or "" when it cannot say."""
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                              timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return " ".join(done.stdout.split())


_ME: dict = {}


def identity() -> dict:
    """This process: machine, process id, start time. Worked out once per process."""
    pid = os.getpid()
    if _ME.get("pid") != pid:
        _ME.clear()
        _ME.update(host=socket.gethostname(), pid=pid, started=_process_start(pid))
    return dict(_ME)


def owner_state(host: str, pid: int, started: str) -> str:
    """"alive", "dead" or "unknown". Unknown -- another machine, or a check that did not work -- is
    never taken for dead: only the claim's expiry frees it then."""
    if host != socket.gethostname():
        return "unknown"
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        pass                                      # it exists, owned by someone else
    except (OSError, ValueError):
        return "unknown"
    now = _process_start(pid)
    if not now:
        return "unknown"
    return "dead" if started and now != started else "alive"   # same number, another program: gone


# -- the tables ---------------------------------------------------------------------------------------

def _engine():
    from data_engineering.db import session as db
    if db._engine is None:
        raise RuntimeError("init_engine() must be called before a preparation is coordinated")
    return db._engine


def ensure_tables() -> None:
    """Create the two tables if they are not there. Two processes may do it at the same moment, the first
    time ever: the one that finds the table made meanwhile ("already exists") simply looks again."""
    from data_engineering.db.models import PreparationBase
    for attempt in range(BUSY_TRIES):
        try:
            PreparationBase.metadata.create_all(_engine())
            return
        except OperationalError as exc:
            if "already exists" not in str(exc).lower() and not _busy(exc):
                raise
            time.sleep(0.05 * (attempt + 1))
    PreparationBase.metadata.create_all(_engine())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)      # naive UTC, as every table here keeps it


def _parse(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromisoformat(str(value).replace("Z", "")).replace(tzinfo=None)


def _busy(exc: Exception) -> bool:
    return isinstance(exc, OperationalError) and "locked" in str(exc).lower()


def _retry(work, unreadable_message: str):
    """Run `work(connection)` in one transaction, asking again while the database is busy; when it stays
    busy, or the claim's row cannot be read, raise LeaseUnreadable -- the safe answer is "held"."""
    last = None
    for attempt in range(BUSY_TRIES):
        try:
            with _engine().begin() as connection:
                return work(connection)
        except (LeaseBusy, LeaseLost, LeaseUnreadable, PublishError, IntegrityError):
            raise
        except OperationalError as exc:
            if not _busy(exc):
                raise LeaseUnreadable(f"{unreadable_message} ({str(exc).splitlines()[0][:120]})") from exc
            last = exc
            time.sleep(0.05 * (attempt + 1))
    raise LeaseUnreadable(f"{unreadable_message}: the database stayed busy ({last})")


def _row(connection, job_id: int) -> dict | None:
    """The job's claim as stored, checked field by field; a damaged row raises LeaseUnreadable."""
    found = connection.execute(text(
        "SELECT job_id, token, purpose, owner_host, owner_pid, owner_started, acquired_at, heartbeat_at, "
        "released_at FROM preparation_leases WHERE job_id = :j"), {"j": job_id}).mappings().first()
    if found is None:
        return None
    try:
        token, pid = found["token"], found["owner_pid"]
        if isinstance(token, bool) or not isinstance(token, int) or token < 1:
            raise ValueError("token")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            raise ValueError("owner")
        beat = _parse(found["heartbeat_at"])
        released = _parse(found["released_at"])
        if beat is None:
            raise ValueError("heartbeat")
    except (TypeError, ValueError) as exc:
        raise LeaseUnreadable(f"This job's preparation claim is damaged ({exc}), so it is treated as held. "
                              "Nothing is prepared for it until the claim is cleared.") from None
    return {**dict(found), "token": token, "heartbeat_at": beat, "released_at": released,
            "raw_heartbeat": found["heartbeat_at"], "raw_released": found["released_at"]}


# -- the claim ------------------------------------------------------------------------------------------

def acquire(job_id: int, purpose: str, *, ttl_s: float = LEASE_TTL_S, now: datetime | None = None) -> Lease:
    """The job's claim, or LeaseBusy / LeaseUnreadable. Taken over only when released, expired, or its
    owner's process is gone; the take-over is a compare-and-set on the token, so two takers cannot
    both win."""
    ensure_tables()
    me = identity()
    for _ in range(BUSY_TRIES):
        stamp = now or _now()

        def work(connection):
            row = _row(connection, job_id)
            values = {"j": job_id, "p": purpose[:64], "h": me["host"], "pid": me["pid"], "s": me["started"],
                      "t": stamp}
            if row is None:
                connection.execute(text(
                    "INSERT INTO preparation_leases (job_id, token, purpose, owner_host, owner_pid, owner_started, "
                    "acquired_at, heartbeat_at, released_at) VALUES (:j, 1, :p, :h, :pid, :s, :t, :t, NULL)"), values)
                return 1
            expired = row["heartbeat_at"] + timedelta(seconds=ttl_s) < stamp
            gone = owner_state(row["owner_host"], row["owner_pid"], row["owner_started"]) == "dead"
            if row["released_at"] is None and not expired and not gone:
                raise LeaseBusy(row["purpose"], row["heartbeat_at"].strftime("%H:%M UTC"))
            changed = connection.execute(text(
                "UPDATE preparation_leases SET token = token + 1, purpose = :p, owner_host = :h, owner_pid = :pid, "
                "owner_started = :s, acquired_at = :t, heartbeat_at = :t, released_at = NULL "
                "WHERE job_id = :j AND token = :old AND heartbeat_at = :beat "
                "AND (released_at IS :rel OR released_at = :rel)"),
                {**values, "old": row["token"], "beat": row["raw_heartbeat"], "rel": row["raw_released"]})
            return row["token"] + 1 if changed.rowcount == 1 else None

        try:
            token = _retry(work, "This job's preparation claim could not be read")
        except IntegrityError:
            continue                                  # another worker inserted it first: look again
        if token is not None:
            return Lease(job_id, token, purpose)
    raise LeaseBusy(purpose, "a moment ago")


HELD = ("EXISTS (SELECT 1 FROM preparation_leases WHERE job_id = :lj AND token = :lk AND released_at IS NULL)")


def assert_held(connection, lease: Lease) -> None:
    row = _row(connection, lease.job_id)
    if row is None or row["token"] != lease.token or row["released_at"] is not None:
        lease.lost = True
        raise LeaseLost("Another preparation took this job over, so what this one made is not used.")


def heartbeat(lease: Lease) -> None:
    def work(connection):
        changed = connection.execute(text(
            "UPDATE preparation_leases SET heartbeat_at = :t WHERE job_id = :j AND token = :k AND released_at IS NULL"),
            {"t": _now(), "j": lease.job_id, "k": lease.token})
        if changed.rowcount != 1:
            lease.lost = True
            raise LeaseLost("Another preparation took this job over.")
    _retry(work, "This job's preparation claim could not be renewed")


def release(lease: Lease) -> None:
    lease._stop.set()

    def work(connection):
        connection.execute(text("UPDATE preparation_leases SET released_at = :t "
                                "WHERE job_id = :j AND token = :k AND released_at IS NULL"),
                           {"t": _now(), "j": lease.job_id, "k": lease.token})
    try:
        _retry(work, "This job's preparation claim could not be released")
    except CoordinationError:
        pass                    # it lapses by itself after LEASE_TTL_S; the next taker finds it expired


def keep_alive(lease: Lease, every_s: float = HEARTBEAT_S) -> threading.Thread:
    """Renew the claim in the background until it is released or lost."""
    def beat():
        while not lease._stop.wait(every_s):
            try:
                heartbeat(lease)
            except LeaseLost:
                return
            except CoordinationError:
                continue                     # busy for a while: the next beat tries again
    thread = threading.Thread(target=beat, name=f"lease-{lease.job_id}", daemon=True)
    thread.start()
    return thread


# -- publication -----------------------------------------------------------------------------------------

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _place(source: Path, target: Path) -> None:
    """Write `target` whole or not at all: a temporary name beside it, flushed to disk, renamed."""
    temp = target.with_name(f".{target.name}.{os.getpid()}.part")
    with open(source, "rb") as read, open(temp, "wb") as write:
        shutil.copyfileobj(read, write)
        write.flush()
        os.fsync(write.fileno())
    os.replace(temp, target)
    os.chmod(target, 0o444)


def publish(lease: Lease, *, docx: Path, pdf: Path, source: str, repeat_key: str, run_id: str | None,
            note: str = "", root: Path | None = None, crash=lambda stage: None) -> Publication:
    """Make this DOCX and PDF the job's newest resume version, or raise. `crash(stage)` lets a test stop
    the process at each step: after the pending row, after each file, before it becomes visible."""
    docx, pdf = Path(docx), Path(pdf)
    if not docx.is_file() or not pdf.is_file():
        raise PublishError("The resume was not made as a DOCX and a PDF, so it was not published.")
    planned = {"docx": sha256(docx), "pdf": sha256(pdf)}
    base = (root or PUBLISHED) / str(lease.job_id)

    def reserve(connection):
        assert_held(connection, lease)
        top = connection.execute(text("SELECT COALESCE(MAX(version), 0) FROM document_publications WHERE job_id = :j"),
                                 {"j": lease.job_id}).scalar()
        version = int(top) + 1
        folder = base / f"v{version}-{lease.token}"
        # Written only while the claim is held, in the same statement (A1.1): no gap between check and write.
        result = connection.execute(text(
            "INSERT INTO document_publications (job_id, version, state, source, repeat_key, run_id, token, docx_path, "
            "pdf_path, docx_sha256, pdf_sha256, created_at, published_at, note) SELECT :j, :v, 'pending', :src, :key, "
            f":run, :k, :dp, :pp, :ds, :ps, :t, NULL, :n WHERE {HELD}"),
            {"j": lease.job_id, "v": version, "src": source, "key": repeat_key, "run": run_id, "k": lease.token,
             "dp": str(folder / "resume.docx"), "pp": str(folder / "resume.pdf"), "ds": planned["docx"],
             "ps": planned["pdf"], "t": _now(), "n": note[:500], "lj": lease.job_id, "lk": lease.token})
        if result.rowcount != 1:
            lease.lost = True
            raise LeaseLost("Another preparation took this job over, so what this one made is not used.")
        row = connection.execute(text("SELECT id FROM document_publications WHERE job_id = :j AND version = :v"),
                                 {"j": lease.job_id, "v": version}).scalar()
        return row, version, folder

    ensure_tables()
    try:
        row_id, version, folder = _retry(reserve, "The new resume version could not be recorded")
    except IntegrityError as exc:              # two versions numbered alike: only a lost claim can do that
        if "unique" not in str(exc).lower():
            raise PublishError(f"The new resume version could not be recorded ({str(exc).splitlines()[0][:120]}).") from exc
        lease.lost = True
        raise LeaseLost("Another preparation recorded a version of this job at the same moment; this one is not "
                        "used.") from exc
    crash("reserved")
    try:
        folder.mkdir(parents=True, exist_ok=False)
        _place(docx, folder / "resume.docx")
        crash("docx")
        _place(pdf, folder / "resume.pdf")
        crash("pdf")
        if sha256(folder / "resume.docx") != planned["docx"] or sha256(folder / "resume.pdf") != planned["pdf"]:
            raise PublishError("The copied resume does not match what was made, so it was not published.")
        crash("before_visible")

        def flip(connection):
            # Visible only if the claim is still this worker's, checked in the same statement (A1.1).
            changed = connection.execute(text(
                "UPDATE document_publications SET state = 'published', published_at = :t "
                f"WHERE id = :i AND state = 'pending' AND {HELD}"),
                {"t": _now(), "i": row_id, "lj": lease.job_id, "lk": lease.token})
            if changed.rowcount != 1:
                assert_held(connection, lease)            # says LeaseLost when that is why
                raise PublishError("The new resume version was settled by something else meanwhile.")
        _retry(flip, "The new resume version could not be made visible")
    except BaseException as exc:
        if not isinstance(exc, _Crash):
            _fail(row_id, f"not published: {type(exc).__name__}: {str(exc)[:200]}")
        raise
    return Publication(row_id, lease.job_id, version, source, repeat_key, run_id, folder / "resume.docx",
                       folder / "resume.pdf", planned["docx"], planned["pdf"], _now(), note)


class _Crash(BaseException):
    """For tests: a process dying at a step, which writes nothing on its way out."""


def _fail(row_id: int, why: str) -> None:
    def work(connection):
        connection.execute(text("UPDATE document_publications SET state = 'failed', note = :n "
                                "WHERE id = :i AND state = 'pending'"), {"n": why[:500], "i": row_id})
    try:
        _retry(work, "A failed resume version could not be marked")
    except CoordinationError:
        pass                                  # recover() settles it later


def _from_row(row) -> Publication:
    return Publication(row["id"], row["job_id"], row["version"], row["source"], row["repeat_key"], row["run_id"],
                       Path(row["docx_path"]), Path(row["pdf_path"]), row["docx_sha256"], row["pdf_sha256"],
                       _parse(row["published_at"]), row["note"] or "")


def current(job_id: int, *, verify: bool = True) -> Publication | None:
    """The job's newest published resume version, or None. With `verify`, both files must still be there
    and match their fingerprints; one that does not is not offered (and an older one is not offered in
    its place: what the person last saw made is the only candidate)."""
    from sqlalchemy import inspect
    engine = _engine()
    if not inspect(engine).has_table("document_publications"):
        return None
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT * FROM document_publications WHERE job_id = :j AND state = 'published' "
            "ORDER BY version DESC LIMIT 1"), {"j": job_id}).mappings().first()
    if row is None:
        return None
    found = _from_row(row)
    # Only files this module placed: a row naming a path outside the published folder (a damaged or edited
    # record) is not followed -- the job page serves these files for download.
    root = PUBLISHED.resolve()
    for path in (found.docx, found.pdf):
        try:
            path.resolve().relative_to(root)
        except (ValueError, OSError):
            return None
    if verify:
        try:
            if sha256(found.docx) != found.docx_sha256 or sha256(found.pdf) != found.pdf_sha256:
                return None
        except OSError:
            return None
    return found


def has_versions(job_id: int) -> bool:
    """Has this job any recorded resume version, published or withdrawn?"""
    from sqlalchemy import inspect
    engine = _engine()
    if not inspect(engine).has_table("document_publications"):
        return False
    with engine.connect() as connection:
        return connection.execute(text("SELECT 1 FROM document_publications WHERE job_id = :j AND state IN "
                                       "('published', 'withdrawn') LIMIT 1"), {"j": job_id}).first() is not None


def latest_for_run(job_id: int, run_id: str) -> Publication | None:
    """The newest published version made from this tailoring run, if any (verified or not)."""
    from sqlalchemy import inspect
    engine = _engine()
    if not inspect(engine).has_table("document_publications"):
        return None
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT * FROM document_publications WHERE job_id = :j AND run_id = :r AND state = 'published' "
            "ORDER BY version DESC LIMIT 1"), {"j": job_id, "r": run_id}).mappings().first()
    return _from_row(row) if row is not None else None


def withdraw(job_id: int, version: int, why: str) -> bool:
    """Take one published version back (a rollback): the one before it becomes current again."""
    def work(connection):
        return connection.execute(text(
            "UPDATE document_publications SET state = 'withdrawn', note = :n WHERE job_id = :j AND version = :v "
            "AND state = 'published'"), {"n": why[:500], "j": job_id, "v": version}).rowcount == 1
    return _retry(work, "The resume version could not be withdrawn")


def recover(*, now: datetime | None = None, ttl_s: float = LEASE_TTL_S) -> dict:
    """After a crash or a restart: every version left pending by a worker that no longer holds its job's
    claim becomes failed, never visible. Safe to run at any time."""
    from sqlalchemy import inspect
    if not inspect(_engine()).has_table("document_publications"):
        return {"failed": 0}
    stamp = now or _now()

    def work(connection):
        pending = connection.execute(text(
            "SELECT id, job_id, token FROM document_publications WHERE state = 'pending'")).mappings().all()
        settled = 0
        for p in pending:
            try:
                row = _row(connection, p["job_id"])
            except LeaseUnreadable:
                continue                                  # cannot tell: left as it is, still never visible
            held = (row is not None and row["token"] == p["token"] and row["released_at"] is None
                    and row["heartbeat_at"] + timedelta(seconds=ttl_s) >= stamp
                    and owner_state(row["owner_host"], row["owner_pid"], row["owner_started"]) != "dead")
            if not held:
                connection.execute(text("UPDATE document_publications SET state = 'failed', note = :n "
                                        "WHERE id = :i AND state = 'pending'"),
                                   {"n": "interrupted before it was published", "i": p["id"]})
                settled += 1
        return settled
    return {"failed": _retry(work, "Interrupted resume versions could not be settled")}
