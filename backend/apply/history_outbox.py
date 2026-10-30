"""Transactional application history, with crash-safe, idempotent JSONL export.

Only newly recorded applications enqueue events. Export never reconstructs history
from jobs, current documents or current queue state. The immutable payload is the
one captured in the transaction that marked the application.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from data_engineering.db.models import ApplyArtifact, ApplyHistoryOutbox, Job
from data_engineering.db.session import get_session


def enqueue(session, job_id: int, entry: dict, now: datetime, destination: Path) -> None:
    """Called inside the state transaction; serialization/insert failures roll it back."""
    job = session.get(Job, job_id)
    at = job.applied_at if job is not None and job.applied_at is not None else now
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    event_id = uuid4().hex
    payload = json.dumps({**entry, "job_id": job_id, "at": at.isoformat(), "history_event_id": event_id})
    session.add(ApplyHistoryOutbox(event_id=event_id, job_id=job_id, payload=payload,
                                   destination=str(Path(destination).resolve())))
    link = entry.get("queue") or {}
    for document in link.get("documents") or []:
        # Only the exact linked versions, not documents prepared later for this item.
        (session.query(ApplyArtifact)
         .filter(ApplyArtifact.item_id == link.get("item_id"), ApplyArtifact.job_id == job_id,
                 ApplyArtifact.path == document.get("made_ready"),
                 ApplyArtifact.sha256 == document.get("sha256"), ApplyArtifact.state == "ready")
         .update({"state": "sent"}, synchronize_session=False))
    session.flush()  # fail inside the state transaction, never after it has committed


def _sync_directory(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(destination: Path, event_id: str, payload: str) -> None:
    """Keep all previous bytes; atomically publish old history plus one complete line.

    A separate, stable flock file serializes processes across os.replace. A crash
    during the temporary write leaves the original intact. A crash after replace
    is detected by the event ID on recovery, even if the DB ack never committed.
    """
    missing = [p for p in (destination.parent, *destination.parent.parents) if not p.exists()]
    for folder in reversed(missing):
        folder.mkdir(exist_ok=True)
        _sync_directory(folder.parent)
    with destination.with_name(destination.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = destination.read_bytes() if destination.exists() else b""
        for line in previous.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue  # never remove/repair pre-existing historical bytes
            if isinstance(row, dict) and row.get("history_event_id") == event_id:
                if row != json.loads(payload):
                    raise ValueError("History event ID exists with a different payload; export refused")
                # A previous process may have died between rename and directory fsync.
                with destination.open("rb") as handle:
                    os.fsync(handle.fileno())
                _sync_directory(destination.parent)
                return
        fd, temp = tempfile.mkstemp(prefix=".history-export-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(previous)
                if previous and not previous.endswith(b"\n"):
                    handle.write(b"\n")
                handle.write(payload.encode("utf-8") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, destination)
            _sync_directory(destination.parent)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)  # only this invocation's unpublished temporary file


def _acknowledge(event_id: str) -> None:
    with get_session() as session:
        (session.query(ApplyHistoryOutbox)
         .filter(ApplyHistoryOutbox.event_id == event_id, ApplyHistoryOutbox.exported_at.is_(None))
         .update({"exported_at": datetime.now(timezone.utc)}, synchronize_session=False))


def export_pending(job_id: int | None = None) -> int:
    """Replay pending events only. Safe for repeated/concurrent calls and process restarts.

    Errors propagate: the event stays pending. No application can be submitted by
    this function, and no existing application lacking an event gets backfilled.
    """
    with get_session() as session:
        query = session.query(ApplyHistoryOutbox).filter(ApplyHistoryOutbox.exported_at.is_(None))
        if job_id is not None:
            query = query.filter(ApplyHistoryOutbox.job_id == job_id)
        events = query.order_by(ApplyHistoryOutbox.created_at, ApplyHistoryOutbox.event_id).all()
        for event in events:
            session.expunge(event)
    for event in events:
        _publish(Path(event.destination), event.event_id, event.payload)
        _acknowledge(event.event_id)
    return len(events)
