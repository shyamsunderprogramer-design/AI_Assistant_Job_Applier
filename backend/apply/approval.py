"""Your approval of each document an application sends (2 Nov 2026).

Each resume and cover letter made ready for a chosen job can be approved or rejected by the person,
one at a time, on the Auto-apply page. The word is kept for exactly that content -- the job, the kind
of document and its checksum (data_engineering.db.models.DocumentApproval) -- so a document changed
in any way afterwards has another checksum and is undecided again: an approval never carries over.

Where it counts:
- A rejected document is never attached, on any run: preparing a job refuses it (runner.prepare_documents)
  and a job made ready before is not sent with it (execution.seal). The job waits for a new one.
- The app presses Submit by itself only with every document approved (execution.documents_unready).
  That adds a condition; it never switches automatic Submit on.
- A document not yet decided goes as before when the person presses Submit.

Only the person's press decides: nothing here approves a document by itself, and a document changed or
gone since it was made ready cannot be decided at all.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from data_engineering.db.models import ApplyArtifact, ApplyItem, DocumentApproval
from data_engineering.db.session import get_session

APPROVED, REJECTED = "approved", "rejected"
DECISIONS = (APPROVED, REJECTED)
KINDS = {"resume": "resume", "cover_letter": "cover letter"}
# A resume cut from the person's own lines (runner.selected_master) is drawn afresh each time, so its
# checksum changes even when its words do not: a rejection of one holds for every later one of that job.
SELECTED = "your resume selected"


class ApprovalError(Exception):
    """The word could not be recorded; the message says why, in the page's words."""


def checksum(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _latest(session, job_id: int, kind: str, sha256: str) -> DocumentApproval | None:
    return (session.query(DocumentApproval)
            .filter(DocumentApproval.job_id == job_id, DocumentApproval.kind == kind,
                    DocumentApproval.sha256 == sha256)
            .order_by(DocumentApproval.id.desc()).first())


def word_on(session, job_id: int, kind: str, sha256: str) -> dict:
    """What the page shows for one document: the person's word on this very content ("approved",
    "rejected" or ""), when, and whether a word was said about an earlier version of it."""
    row = _latest(session, job_id, kind, sha256)
    earlier = row is None and session.query(DocumentApproval.id).filter(
        DocumentApproval.job_id == job_id, DocumentApproval.kind == kind,
        DocumentApproval.sha256 != sha256).first() is not None
    return {"approval": row.decision if row else "",
            "approval_at": row.decided_at.replace(tzinfo=timezone.utc).isoformat() if row else "",
            "approval_earlier": earlier}


def decision(job_id: int, kind: str, path) -> str:
    """The person's word on the file at `path` for this job: "approved", "rejected" or ""."""
    sha = checksum(path)
    with get_session() as session:
        row = _latest(session, job_id, kind, sha)
        return row.decision if row else ""


def rejected_selection(job_id: int) -> bool:
    """Whether the person's latest word on a resume selected from their own lines, for this job, is No."""
    with get_session() as session:
        row = (session.query(DocumentApproval)
               .filter(DocumentApproval.job_id == job_id, DocumentApproval.kind == "resume",
                       DocumentApproval.source.like(SELECTED + "%"))
               .order_by(DocumentApproval.id.desc()).first())
        return row is not None and row.decision == REJECTED


def refused(job_id: int, kind: str, path, source: str | None = None) -> str | None:
    """Why this document may not go with any application, or None: the person rejected it."""
    what = KINDS.get(kind, kind)
    if decision(job_id, kind, path) == REJECTED:
        return f"you rejected its {what} ({Path(path).name}); make a new one on its job page"
    if kind == "resume" and (source or "").startswith(SELECTED) and rejected_selection(job_id):
        return "you rejected the resume selected from your own lines for it; tailor one on its job page"
    return None


def unapproved(job_id: int, kind: str, path) -> str | None:
    """Why the app may not send this document by itself, or None: approved by the person."""
    word = decision(job_id, kind, path)
    if word == APPROVED:
        return None
    what = KINDS.get(kind, kind)
    return f"you rejected its {what}" if word == REJECTED else f"you have not approved its {what} yet"


def decide(item_id: int, artifact_id: int, word: str) -> dict:
    """The person approves or rejects one document made ready for one chosen job. Only a document made
    ready and not yet sent, still exactly as it was made ready, can be decided."""
    from backend.apply import execution
    if word not in DECISIONS:
        raise ApprovalError('Say "approved" or "rejected".')

    @execution._when_free
    def record() -> dict:
        with get_session() as session:
            row = session.get(ApplyArtifact, artifact_id)
            item = session.get(ApplyItem, item_id)
            if row is None or item is None or row.item_id != item_id:
                raise ApprovalError("That document is not one made ready for this job.")
            what = KINDS.get(row.kind, row.kind)
            if row.state != "ready":
                raise ApprovalError(f"This {what} is no longer the one its form would be given "
                                    f"({row.state}): only a document made ready and not yet sent can be decided.")
            path, origin = Path(row.path), Path(row.origin_path) if row.origin_path else None
            try:
                same = checksum(path) == row.sha256 and (
                    origin is None or checksum(origin) == row.origin_sha256)
            except OSError:
                same = False
            if not same:
                raise ApprovalError(f"This {what} has changed or is gone since it was made ready, so it "
                                    "cannot be decided: Retry makes its documents ready again.")
            session.add(DocumentApproval(job_id=row.job_id, kind=row.kind, sha256=row.sha256, name=path.name,
                                         source=row.source, artifact_id=row.id, decision=word,
                                         decided_at=datetime.now(timezone.utc).replace(tzinfo=None)))
            said = "approved" if word == APPROVED else "rejected"
            execution._event(session, f"You {said} its {what} {path.name} (checksum {row.sha256[:12]})"
                             + ("" if word == APPROVED else ": it will not be sent"), item=item)
            session.flush()
            return {"artifact": row.id, "kind": row.kind, **word_on(session, row.job_id, row.kind, row.sha256)}

    return record()
