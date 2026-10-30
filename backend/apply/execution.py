"""The chosen jobs' queue: kept in the database, gone through one at a time, never lost.

Added 24 Oct 2026 (Auto Apply v3.0, phase 1). The jobs the person ticked on the Jobs page and added
to Auto-apply wait here (table apply_items), each in one of these states:

    queued                waiting its turn
    validating            checked again just before it goes: still open, still theirs to apply to,
                          the profile still allows it, the day's limit, the company's turn
    preparing             its resume (tailored if need be) and cover letter made ready
    ready                 everything ready; the form opens next
    running               the app's browser has the form open and is filling it
    waiting_user          the form waits for the person: a CAPTCHA, a question, a sign-in, Submit
    pending_confirmation  it may have gone and the app cannot tell: only the person can say
    applied               a confirmation page was seen, or the person said it went
    failed                it could not go (the reason is kept); Retry puts it back
    skipped               the person closed the form, or it cannot go any more (the reason is kept)
    cancelled             taken off by the person before it went

Only the moves in MOVES are made, and each one is written down (apply_events). A job is never
taken twice at once: one live entry per job (the table's own unique index), one run at a time
(apply_runs), and an entry is taken by a conditional update, so two processes asking for the same
one cannot both have it. Nothing that may already have been sent is ever tried again by itself: a
form left open by a crash, or with no answer in time, becomes pending_confirmation, and only the
person moves it on.

A run goes down the queue by the rule the Jobs page's review shows (runner.chosen_queue): best fit
first, one job a company, twelve hours apart, up to the day's limit. Pause stops it before the
next form opens; a form on screen is finished or closed first.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.config.loader import PROJECT_ROOT
from data_engineering.db.models import FINISHED_STATES, ApplyArtifact, ApplyEvent, ApplyItem, ApplyRun, Job
from data_engineering.db.session import get_session

log = logging.getLogger(__name__)

# While this file exists no run presses Submit by itself, whatever the person's own setting says
# (29 Oct 2026: the Greenhouse checks were found to fail open; the fixes go live only when approved).
# Deleting it lifts the hold; the setting itself is never changed.
AUTO_SUBMIT_HOLD = PROJECT_ROOT / "data" / "apply" / "auto_submit.hold"


def auto_submit_held() -> bool:
    return AUTO_SUBMIT_HOLD.exists()

QUEUED, VALIDATING, PREPARING, READY, RUNNING = "queued", "validating", "preparing", "ready", "running"
WAITING, PENDING = "waiting_user", "pending_confirmation"
APPLIED, FAILED, SKIPPED, CANCELLED = "applied", "failed", "skipped", "cancelled"
FINISHED = frozenset(FINISHED_STATES)
ON_ITS_WAY = (VALIDATING, PREPARING, READY, RUNNING, WAITING)    # in a run's hands
FORM_OPEN = (RUNNING, WAITING)                                    # its form open in the app's browser
LIVE = (QUEUED, *ON_ITS_WAY, PENDING)                             # on the list, not finished

LABELS = {QUEUED: "Queued", VALIDATING: "Validating", PREPARING: "Preparing", READY: "Ready",
          RUNNING: "Running", WAITING: "Waiting for you", PENDING: "Pending confirmation",
          APPLIED: "Applied", FAILED: "Failed", SKIPPED: "Skipped", CANCELLED: "Cancelled"}

# Where each state may go next. APPLIED from a state before the form is "applied another way":
# on the job's own page, or an employer's email found it (sync_with_jobs).
MOVES = {
    QUEUED: {VALIDATING, CANCELLED, APPLIED},
    VALIDATING: {PREPARING, QUEUED, SKIPPED, FAILED, CANCELLED, APPLIED},
    PREPARING: {READY, QUEUED, FAILED, SKIPPED, CANCELLED, APPLIED},
    READY: {RUNNING, QUEUED, SKIPPED, CANCELLED, APPLIED},
    RUNNING: {WAITING, PENDING, APPLIED, FAILED, SKIPPED, QUEUED},
    WAITING: {PENDING, APPLIED, FAILED, SKIPPED},
    PENDING: {APPLIED, FAILED},
    APPLIED: set(),
    FAILED: {QUEUED},
    SKIPPED: {QUEUED},
    CANCELLED: {QUEUED},
}

APPLIED_STATUSES = ("Applied", "Interviewing", "Offer", "Rejected")
SETTLED = ("This was answered a moment ago (in another tab, or on the job's own page) or settled by the "
           "run: it is {} now. Nothing was changed.")
CHECK_IF_SENT = ("Check whether it was sent (the site, or a confirmation email), then say: "
                 "Yes, it went, or No, it wasn't sent.")


class QueueError(ValueError):
    """A move the queue cannot make, in words the person can act on."""


class Conflict(QueueError):
    """Another process moved it first. The move is not made, and nothing is wrong: the other move
    stands. Only this is passed over quietly; a move MOVES does not allow is a mistake, and says so."""


class Busy(QueueError):
    """Another run is going down the list."""


def _when_free(fn):
    """Try again, briefly, when SQLite says the database is locked. Two processes writing at once
    -- the web page and a run -- otherwise turn a moment's wait into an error: in WAL mode a write
    that began on an older snapshot is refused outright rather than queued."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        for attempt in range(10):
            try:
                return fn(*args, **kwargs)
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))
    return wrapper


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plain(when: datetime) -> datetime:
    """SQLite keeps no time zone: stored as UTC, compared as UTC."""
    return when.astimezone(timezone.utc).replace(tzinfo=None)


def _event(session, message: str, *, item: ApplyItem | None = None, run_id: int | None = None,
           state: str | None = None) -> None:
    session.add(ApplyEvent(item_id=item.id if item else None, job_id=item.job_id if item else None,
                           run_id=run_id if run_id is not None else (item.run_id if item else None),
                           state=state, message=message[:600], at=_plain(_now())))


# -- the moves ---------------------------------------------------------------------------------

@_when_free
def move(item_id: int, to: str, *, note: str | None = None, action: str | None = None,
         outcome: str | None = None, run_id: int | None = None, expect=None,
         message: str | None = None, then=None) -> ApplyItem:
    """Move one entry to `to`, if MOVES allows it from where it stands now (and, given `expect`, only
    from that state). Its note and action are replaced; written down with the reason. A move made
    meanwhile by another process wins, and this one raises Conflict; a move MOVES does not allow
    raises QueueError. `then(session)` runs in the same transaction once the move is taken, so what
    it writes stands or falls with the move."""
    with get_session() as session:
        item = session.get(ApplyItem, item_id)
        if item is None:
            raise QueueError("That job is not on your list.")
        here = item.state
        if expect is not None and here not in ({expect} if isinstance(expect, str) else set(expect)):
            raise Conflict(f"It is {LABELS[here].lower()} now.")
        if to not in MOVES[here]:
            raise QueueError(f"A job that is {LABELS[here].lower()} cannot become {LABELS[to].lower()}.")
        now = _plain(_now())
        values: dict = {"state": to, "note": note, "action": action, "updated_at": now,
                        "finished_at": now if to in FINISHED else None}
        if outcome is not None or to == QUEUED:
            values["outcome"] = outcome
        if run_id is not None:
            values["run_id"] = run_id
        if to == VALIDATING:
            values["attempts"] = ApplyItem.attempts + 1
        taken = (session.query(ApplyItem).filter(ApplyItem.id == item_id, ApplyItem.state == here)
                 .update(values, synchronize_session=False))
        if taken != 1:
            raise Conflict("It moved on meanwhile.")
        if then is not None:
            then(session)
        session.flush()
        session.refresh(item)
        detail = note or action or outcome
        # "Applied ✓ (...)" says its state already: not "Applied: Applied ✓ (...)".
        words = message or (detail if detail and detail.lower().startswith(LABELS[to].lower())
                            else f"{LABELS[to]}: {detail}" if detail else LABELS[to])
        _event(session, words, item=item, state=to)
        session.expunge(item)
        return item


@_when_free
def note(item_id: int, message: str, *, run_id: int | None = None) -> None:
    """A line of what is happening to a job, for the activity list; its state stays as it is."""
    with get_session() as session:
        item = session.get(ApplyItem, item_id)
        if item is not None:
            _event(session, message, item=item, run_id=run_id)


# -- the list ----------------------------------------------------------------------------------

@_when_free
def add(job_ids) -> tuple[list[int], list[int]]:
    """Put these jobs at the end of the queue. Returns (added, on the list already): a job on its way
    is never added twice, whoever asks (the table's unique index has the last word)."""
    added, already = [], []
    try:
        with get_session() as session:
            live = {j for (j,) in session.query(ApplyItem.job_id).filter(ApplyItem.state.notin_(FINISHED))}
            top = session.query(func.max(ApplyItem.position)).scalar() or 0
            for job_id in dict.fromkeys(job_ids):
                if job_id in live:
                    already.append(job_id)
                    continue
                top += 1
                item = ApplyItem(job_id=job_id, state=QUEUED, position=top, added_at=_plain(_now()),
                                 updated_at=_plain(_now()))
                session.add(item)
                session.flush()
                _event(session, "Added to your chosen jobs", item=item, state=QUEUED)
                live.add(job_id)
                added.append(job_id)
    except IntegrityError:
        raise QueueError("One of those jobs was added from somewhere else meanwhile. Try again.")
    return added, already


def known_job_ids() -> set[int]:
    """Every job that has ever been on the list, finished or not."""
    with get_session() as session:
        return {j for (j,) in session.query(ApplyItem.job_id).distinct()}


def live_job_ids(states=(QUEUED,)) -> list[int]:
    """The jobs in these states, in the order they were added."""
    with get_session() as session:
        return [j for (j,) in session.query(ApplyItem.job_id).filter(ApplyItem.state.in_(states))
                .order_by(ApplyItem.position)]


CANT_CANCEL = {
    RUNNING: "Its form is open in the app's browser and may be on its way: finish it or close it there.",
    WAITING: "Its form is open in the app's browser, waiting for you: finish it or close it there.",
    PENDING: "It may have gone already: say whether it was sent instead.",
}


def cancel(item_id: int) -> ApplyItem:
    """Taken off before it goes. A form already open is never pulled from under the person -- it may
    be being sent that moment: it is theirs to finish or close, and the run records how it ended. A
    run that took it meanwhile finds it cancelled at its next step and never opens its form."""
    try:
        return move(item_id, CANCELLED, outcome="Cancelled by you: nothing was sent.",
                    expect=(QUEUED, VALIDATING, PREPARING, READY))
    except Conflict as exc:
        with get_session() as session:
            item = session.get(ApplyItem, item_id)
            state = item.state if item is not None else ""
        raise Conflict(CANT_CANCEL.get(state, str(exc))) from None


def cancel_waiting() -> int:
    """Every job still waiting its turn, taken off."""
    count = 0
    for _, item_id in _items_in(QUEUED):
        try:
            cancel(item_id)
            count += 1
        except QueueError:
            pass                                    # moved on meanwhile: left as it is
    return count


def retry(item_id: int) -> ApplyItem:
    """Back in the queue: only a job that failed, was skipped or cancelled, and is not applied to
    since. One that may have gone (pending confirmation) needs the person's word first."""
    with get_session() as session:
        item = session.get(ApplyItem, item_id)
        if item is None:
            raise QueueError("That job is not on your list.")
        if item.state == PENDING:
            raise QueueError("It may have gone already. Say first whether it was sent.")
        job = session.get(Job, item.job_id)
        if job is not None and job.status in APPLIED_STATUSES:
            raise QueueError(f"It's marked {job.status}: there is nothing to try again.")
    try:
        return move(item_id, QUEUED, note="Back in the queue: try again", expect=(FAILED, SKIPPED, CANCELLED))
    except IntegrityError:
        raise QueueError("It is on your list already.")


def confirm(item_id: int, went: bool) -> ApplyItem:
    """The person's word on a job the app could not see sent: recorded as an application, or as not
    sent (then Retry may put it back). Taken once (`answer`)."""
    from backend.apply import anyform
    with get_session() as session:
        item = session.get(ApplyItem, item_id)
        if item is None:
            raise QueueError("That job is not on your list.")
        if item.state not in (PENDING, WAITING):             # a page drawn before it was settled
            raise Conflict(SETTLED.format(LABELS[item.state].lower()))
        if item.state == WAITING and run_alive():
            raise QueueError("Its form is still open in the app's browser: finish it or close it there.")
        job = session.get(Job, item.job_id)
        job_id, company, title = item.job_id, job.company if job else "", job.title if job else ""
    entry = anyform.unconfirmed(job_id) or {"company": company, "title": title, "how": "you",
                                            "confirmed": "by you, on the Auto-apply page"}
    settled = answer(item_id, job_id, went, entry, expect=(PENDING, WAITING),
                     outcome=f"Applied: you said it went ({_clock(_now())})." if went
                     else "Not sent: you said so. Retry puts it back in the queue.")
    anyform.forget_unconfirmed(job_id)
    return settled


def answer(item_id: int, job_id: int, went: bool, entry: dict, *, expect, outcome: str) -> ApplyItem:
    """The person's word on a job that may have gone, taken once (25 Oct 2026). Its entry here is
    claimed and, for a yes, the job marked applied, in one transaction: a second answer -- another
    tab, the job's own page, a double press -- finds it settled, is refused, and changes nothing.
    (Recorded first and claimed after, a yes and a no at the same moment could leave the list saying
    Failed and the job marked Applied.) Its immutable pending history and sent-document state are
    committed with the claim. JSONL export may be retried without changing that original record."""
    from backend.apply import runner
    from backend.apply.history_outbox import enqueue
    from backend.apply.runner import mark_applied, write_audit
    now, marked = _now(), []
    entry = {**entry, "queue": entry.get("queue") or _link(item_id)}      # where it came from on this list

    def record(session):
        marked.append(mark_applied(session, job_id, now))
        if marked[-1] is not False:
            enqueue(session, job_id, {k: v for k, v in entry.items() if k != "kept_at"}, now, runner.AUDIT_LOG)
        (session.query(ApplyArtifact).filter(ApplyArtifact.item_id == item_id, ApplyArtifact.state == "ready")
         .update({"state": "sent"}, synchronize_session=False))
    try:
        if went:
            item = move(item_id, APPLIED, outcome=outcome, expect=expect,
                        then=record)
        else:
            item = move(item_id, FAILED, outcome=outcome, expect=expect)
    except Conflict:
        with get_session() as session:
            found = session.get(ApplyItem, item_id)
            state = found.state if found is not None else ""
        raise Conflict(SETTLED.format(LABELS.get(state, "settled").lower())) from None
    if marked and marked[-1] is not False:                  # the last try is the one that stood
        write_audit(job_id, {k: v for k, v in entry.items() if k != "kept_at"}, now)
    return item


def settled_elsewhere(job_id: int, went: bool, entry: dict | None = None) -> ApplyItem | None:
    """The person answered "Did it go?" on the job's own page: its entry here, if one waits for that
    answer, is settled the same once-only way (`answer`), the job's record with it. None when it has
    none -- the page records the job itself. A form still open in a run is left to the run, which
    records how it ends. Conflict when another answer settled it first."""
    states = (PENDING,) if run_alive() else (PENDING, WAITING)
    waiting = _items_in(*states, job_id=job_id)
    if not waiting:
        return None
    return answer(waiting[0][1], job_id, went, entry or {"how": "you", "confirmed": "by you, on its page"},
                  expect=states, outcome="Applied: you said it went, on its page." if went
                  else "Not sent: you said so, on its page. Retry puts it back in the queue.")


@_when_free
def hide_finished() -> int:
    """Finished jobs off the page; kept in the database with what happened to them."""
    with get_session() as session:
        return (session.query(ApplyItem).filter(ApplyItem.state.in_(FINISHED), ApplyItem.hidden.is_(False))
                .update({"hidden": True}, synchronize_session=False))


@_when_free
def decided(item_id: int, decision, *, run_id: int | None = None, always: bool = False) -> bool:
    """Keep this entry's latest eligibility decision (an eligibility.Decision, or its as_dict()), and
    write it down as a step when it differs from the one kept -- another decision, other reasons, newer
    rules -- or always, as a run's check just before a job goes. Its state stays as it is: a decision
    says whether and how a job may go, not where it stands."""
    detail = decision if isinstance(decision, dict) else decision.as_dict()
    with get_session() as session:
        item = session.get(ApplyItem, item_id)
        if item is None:
            return False
        kept = json.loads(item.decision_detail) if item.decision_detail else {}
        if not always and all(kept.get(k) == detail[k] for k in ("decision", "reasons", "rule_version")):
            return False
        item.decision, item.decision_detail = detail["decision"], json.dumps(detail)
        words = f"Eligibility: {detail['label']} — {'; '.join(detail['reasons'])}"
        if item.state == QUEUED and (item.note or "").startswith("Waits for your answer:") \
                and detail["decision"] != "NEEDS_INFORMATION":
            item.note = words                                # answered since: no longer waiting for it
        _event(session, words, item=item, run_id=run_id)
        return True


def keep_decisions(decisions: dict) -> int:
    """Each job's decision (job id -> Decision or its dict) kept on its entry on the list; how many changed."""
    with get_session() as session:
        live = {j: i for j, i in session.query(ApplyItem.job_id, ApplyItem.id).filter(ApplyItem.state.in_(LIVE))}
    return sum(decided(live[j], d) for j, d in decisions.items() if j in live)


# -- the documents (26 Oct 2026, phase 3) ----------------------------------------------------------
#
# Each job's resume and cover letter are written down when they are made ready -- the file, its
# checksum, where it was made from and that file's checksum, the run and the attempt -- and the
# form is given exactly that content: copied into the attempt's own folder and checked against the
# checksum just before the form opens. One changed or gone since (the resume tailored again on the
# job's page, the letter edited) is never sent: the job fails with the reason, and Retry makes its
# documents ready again.

KINDS = {"resume": "resume", "cover_letter": "cover letter"}


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _artifact_dict(row: ApplyArtifact) -> dict:
    return {"id": row.id, "kind": row.kind, "path": row.path, "name": Path(row.path).name,
            "sha256": row.sha256, "size": row.size, "source": row.source or "",
            "origin": row.origin_path or "", "origin_sha256": row.origin_sha256 or "",
            "run_id": row.run_id, "attempt": row.attempt, "state": row.state, "at": _utc(row.created_at)}


_NOT_SHOWN = ("path", "origin", "origin_sha256")           # this Mac's folders: the page needs only names


@_when_free
def record_documents(item_id: int, ready, *, run_id: int | None = None) -> list[dict]:
    """Write down exactly which resume and cover letter this job sends (a runner.Prepared), and say so
    as a step. What it was made ready with before is replaced."""
    origins = getattr(ready, "origins", None) or {}
    files = [("resume", ready.kept)] + ([("cover_letter", ready.letter)] if ready.letter else [])
    found = []
    for kind, path in files:
        origin = origins.get(kind)
        origin_sum = _sha256(origin) if origin and Path(origin).is_file() else None
        found.append((kind, Path(path), _sha256(path), Path(path).stat().st_size,
                      str(origin) if origin_sum else None, origin_sum))
    with get_session() as session:
        item = session.get(ApplyItem, item_id)
        if item is None:
            return []
        (session.query(ApplyArtifact).filter(ApplyArtifact.item_id == item_id, ApplyArtifact.state == "ready")
         .update({"state": "replaced"}, synchronize_session=False))
        rows = [ApplyArtifact(item_id=item_id, job_id=item.job_id, attempt=item.attempts, kind=kind,
                              run_id=run_id if run_id is not None else item.run_id, path=str(path), sha256=sha,
                              size=size, source=ready.resume_note if kind == "resume" else None,
                              origin_path=origin, origin_sha256=origin_sum, created_at=_plain(_now()))
                for kind, path, sha, size, origin, origin_sum in found]
        session.add_all(rows)
        session.flush()
        words = " and ".join(f"{KINDS[kind]} {path.name} (checksum {sha[:12]})" for kind, path, sha, *_ in found)
        _event(session, f"Documents ready: {words} · {ready.resume_note}", item=item, run_id=run_id)
        return [_artifact_dict(r) for r in rows]


def documents(item_id: int) -> list[dict]:
    """The documents made ready for this job and not replaced since: what its next form is given."""
    with get_session() as session:
        return [_artifact_dict(r) for r in session.query(ApplyArtifact)
                .filter(ApplyArtifact.item_id == item_id, ApplyArtifact.state == "ready").order_by(ApplyArtifact.id)]


def _documents_of(session, item_ids) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    ids = [i for i in item_ids if i]
    if ids:
        for row in (session.query(ApplyArtifact).filter(ApplyArtifact.item_id.in_(ids),
                                                         ApplyArtifact.state.in_(("ready", "sent")))
                    .order_by(ApplyArtifact.id)):
            out.setdefault(row.item_id, []).append({k: v for k, v in _artifact_dict(row).items()
                                                    if k not in _NOT_SHOWN})
    return out


def answers_given(answers: list[dict], applicant=None) -> int:
    """Answers given on the Questions page ([{label, value, job_ids}]): each chosen job waiting on the
    list that they were for says so -- its note replaced, as a step -- and what of its form is still
    open for the person. How many jobs."""
    from backend.apply import runner
    per_job: dict[int, int] = {}
    for a in answers:
        if str(a.get("value") or "").strip():
            for job_id in a.get("job_ids") or []:
                per_job[int(job_id)] = per_job.get(int(job_id), 0) + 1
    told = 0
    for job_id, count in per_job.items():
        job = _job(job_id)
        left = runner.questions_waiting(job, applicant) if job is not None and applicant is not None else None
        words = f"You answered {count} of its form's question{'s' if count != 1 else ''} on the Questions page" + (
            "" if left is None else f"; {len(left)} required still open" if left
            else "; nothing required is left: it goes when its turn comes")
        for _, item_id in _items_in(QUEUED, job_id=job_id):
            told += _renote(item_id, words)
    return told


@_when_free
def _renote(item_id: int, words: str, *, run_id: int | None = None) -> bool:
    """A waiting job's note replaced -- its state as it is -- and written down as a step."""
    with get_session() as session:
        changed = (session.query(ApplyItem).filter(ApplyItem.id == item_id, ApplyItem.state == QUEUED)
                   .update({"note": words, "updated_at": _plain(_now())}, synchronize_session=False))
        if changed:
            _event(session, words, item=session.get(ApplyItem, item_id), run_id=run_id)
        return bool(changed)


def _assisted(job) -> str:
    from backend.apply import runner
    return runner.assisted(job)


def _redo(job_id: int) -> str | None:
    """Why the job's tailored resume is to be made again before it goes out -- made from a master since
    repaired (ml/tailoring/pipeline.py) -- or None: documents made ready from it are not sent as they are."""
    from ml.tailoring.pipeline import needs_redo
    return needs_redo(job_id)


def _link(item_id: int, sent: dict | None = None) -> dict:
    """Where an application came from on this list, for its line in data/applications.jsonl and its
    Applied page: the entry, the run, the attempt, and each document that went -- its name, its
    checksum, and the file it was given from (`sent`: kind -> the copy in the attempt's folder)."""
    with get_session() as session:
        item = session.get(ApplyItem, item_id)
        if item is None:
            return {}
        rows = (session.query(ApplyArtifact).filter(ApplyArtifact.item_id == item_id, ApplyArtifact.state == "ready")
                .order_by(ApplyArtifact.id).all())
        return {"item_id": item.id, "run_id": item.run_id, "attempt": item.attempts,
                "documents": [{"kind": a.kind, "name": Path(a.path).name, "sha256": a.sha256,
                               "made_ready": a.path, "made_ready_in_run": a.run_id,
                               **({"sent": str((sent or {})[a.kind])} if (sent or {}).get(a.kind) else {})}
                              for a in rows]}


def documents_unready(job_id: int, resume: Path, letter: Path | None) -> str | None:
    """Why the documents about to go are not ready for the app to send by itself, or None (29 Oct 2026).
    A checksum shows a file is unchanged, not that it is good: the resume must be the very file a tailoring
    run made and checked -- every check passed, no problem noted in its drafting, not set to be made again --
    and a cover letter, whose checks are not kept anywhere, leaves Submit to the person. Nothing records the
    person's approval of a document yet (Phase 4), so this is the quality half only."""
    from ml.tailoring import pipeline as tailoring
    if _redo(job_id):
        return "its resume is set to be made again"
    if letter is not None:
        return "a cover letter goes with it, and a letter's checks are not kept"
    run, _ = tailoring.job_best(job_id)
    folder = tailoring.RUNS / run if run else None
    try:
        result = json.loads((folder / "result.json").read_text()) if folder else None
    except (OSError, ValueError):
        result = None
    if result is None:
        return "its resume has no checked tailoring run to vouch for it"
    made = {_sha256(p) for p in (folder / "tailored-resume.pdf", folder / "tailored-resume.docx") if p.exists()}
    if _sha256(resume) not in made:
        return "the resume about to go is not the one its checked tailoring run made"
    if not result.get("checks_ok") or any(c.get("severity") == "problem" for c in result.get("checks") or []):
        return "its resume did not pass all of its own checks"
    if result.get("draft_note"):
        return f"its tailoring noted a problem ({str(result['draft_note'])[:80]})"
    return None


@_when_free
def _sent(item_id: int) -> int:
    """Its documents went with the application: kept as sent, its record of them."""
    with get_session() as session:
        return (session.query(ApplyArtifact).filter(ApplyArtifact.item_id == item_id, ApplyArtifact.state == "ready")
                .update({"state": "sent"}, synchronize_session=False))


def _gone_stale(item_id: int) -> None:
    with get_session() as session:
        (session.query(ApplyArtifact).filter(ApplyArtifact.item_id == item_id, ApplyArtifact.state == "ready")
         .update({"state": "stale"}, synchronize_session=False))


def seal(item_id: int, evidence: Path):
    """This job's documents for its form: each copied into this attempt's own folder (`evidence`,
    send/ under the name the employer sees) and checked there against its checksum, and the file it
    was made from checked as well. Returns (resume, cover letter or None, where the resume came from),
    or -- one changed or gone since it was made ready -- why nothing is sent; those are never used again."""
    recorded = documents(item_id)
    resume = next((d for d in recorded if d["kind"] == "resume"), None)
    if resume is None:
        return "no resume was made ready for it"
    sealed = {}
    for d in recorded:
        source, what = Path(d["path"]), KINDS[d["kind"]]
        target = evidence / "send" / source.name
        try:
            if source.resolve() != target.resolve():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            same = _sha256(target) == d["sha256"]
        except OSError:
            same = False
        origin = Path(d["origin"]) if d["origin"] else None
        moved = origin is not None and (not origin.is_file() or _sha256(origin) != d["origin_sha256"])
        if not same or moved:
            _gone_stale(item_id)
            where = f"{origin.name}, the job's own" if moved and origin is not None else source.name
            return (f"the {what} made ready for it ({where}) has changed or is gone since it was made "
                    f"ready on {d['at'][:10]}, so it was not sent")
        sealed[d["kind"]] = target
        # Beside the run's form picture, as every run keeps it: what went out (backend/apply/tracker.py).
        plain = evidence / (f"resume{target.suffix}" if d["kind"] == "resume" else f"cover-letter{target.suffix}")
        if plain.resolve() != target.resolve():
            shutil.copy2(target, plain)
    return sealed["resume"], sealed.get("cover_letter"), resume["source"]


def _items_in(*states, job_id: int | None = None) -> list[tuple[int, int]]:
    with get_session() as session:
        query = session.query(ApplyItem.job_id, ApplyItem.id).filter(ApplyItem.state.in_(states))
        if job_id is not None:
            query = query.filter(ApplyItem.job_id == job_id)
        return [(j, i) for j, i in query.order_by(ApplyItem.position)]


def _elsewhere(status: str) -> str:
    return f"Marked {status} another way (its own page, or your email)."


def sync_with_jobs() -> int:
    """A job applied to another way -- its own page, or an employer's email -- is Applied here too.
    Only from the state it was read in: one a run has taken to its form since is left to the run."""
    with get_session() as session:
        rows = (session.query(ApplyItem.id, ApplyItem.state, Job.status)
                .join(Job, Job.id == ApplyItem.job_id)
                .filter(ApplyItem.state.in_((QUEUED, VALIDATING, PREPARING, READY, PENDING)),
                        Job.status.in_(APPLIED_STATUSES)).all())
    count = 0
    for item_id, state, status in rows:
        try:
            move(item_id, APPLIED, outcome=_elsewhere(status), expect=state)
            count += 1
        except QueueError:
            pass
    return count


# -- runs ----------------------------------------------------------------------------------------

def _started(pid: int) -> datetime | None:
    """When a process started, from `ps`; None when it cannot be told."""
    try:
        text = " ".join(subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                                       text=True, timeout=5).stdout.split())    # "Oct  4": one space
        return datetime.strptime(text, "%a %b %d %H:%M:%S %Y").astimezone(timezone.utc) if text else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _alive(run: ApplyRun) -> bool:
    """Is this run's own process still going? A pid can be given to another process after a crash or
    a restart of the Mac: one started well after the run began is not the run's."""
    if not run.pid:
        return False
    try:
        os.kill(run.pid, 0)
    except OSError:
        return False
    began = _started(run.pid)
    started_at = run.started_at.replace(tzinfo=timezone.utc)
    if began is not None and began > started_at + timedelta(minutes=2):
        return False
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(run.pid)], capture_output=True,
                               text=True, timeout=5).stdout.strip()
        return not state.startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return True


def current_run() -> ApplyRun | None:
    """The run going now (or pausing), if its process is alive."""
    with get_session() as session:
        runs = session.query(ApplyRun).filter(ApplyRun.state.in_(("running", "pausing"))).all()
        for run in runs:
            session.expunge(run)
    return next((r for r in runs if _alive(r)), None)


def run_alive() -> bool:
    return current_run() is not None


@_when_free
def start_run(pid: int) -> ApplyRun:
    """A new run, refused while another is going: one browser, one run. Two processes starting at
    the same moment cannot both have one: the table holds one live run at most (its own unique
    index), so the second insert is refused, whatever either of them looked at first."""
    recover()
    if run_alive():
        raise Busy("Your chosen jobs are being applied to already.")
    try:
        with get_session() as session:
            run = ApplyRun(state="running", pid=pid, started_at=_plain(_now()), heartbeat_at=_plain(_now()))
            session.add(run)
            session.flush()
            _event(session, "Started going down your chosen jobs", run_id=run.id)
            session.expunge(run)
            return run
    except IntegrityError:
        raise Busy("Your chosen jobs are being applied to already.")


@_when_free
def heartbeat(run_id: int) -> None:
    with get_session() as session:
        session.query(ApplyRun).filter(ApplyRun.id == run_id).update({"heartbeat_at": _plain(_now())})


@_when_free
def finish_run(run_id: int, state: str, why: str) -> None:
    with get_session() as session:
        session.query(ApplyRun).filter(ApplyRun.id == run_id, ApplyRun.state.in_(("running", "pausing"))) \
            .update({"state": state, "finished_at": _plain(_now()), "note": why})
        _event(session, why, run_id=run_id)


@_when_free
def request_pause() -> bool:
    """Stop before the next form opens. True when a run was going to be told."""
    run = current_run()
    if run is None:
        return False
    with get_session() as session:
        changed = session.query(ApplyRun).filter(ApplyRun.id == run.id, ApplyRun.state == "running") \
            .update({"state": "pausing"})
        if changed:
            _event(session, "Pausing: the form on screen is finished or closed first", run_id=run.id)
    return bool(changed)


def pause_asked(run_id: int) -> bool:
    with get_session() as session:
        run = session.get(ApplyRun, run_id)
        return run is not None and run.state == "pausing"


def recover() -> dict:
    """After a crash, or a restart of the app or the Mac. A run whose process is gone is marked
    interrupted. Its jobs whose form had not opened go back in the queue: nothing was sent. Those
    whose form was open become pending confirmation, for the person to say whether they went:
    never tried again by themselves. Safe to call at any time."""
    from backend.apply.history_outbox import export_pending
    export_pending()
    fixed = {"runs": 0, "requeued": 0, "to_confirm": 0}
    with get_session() as session:
        runs = session.query(ApplyRun).filter(ApplyRun.state.in_(("running", "pausing"))).all()
        for run in runs:
            session.expunge(run)
    for run in runs:
        if not _alive(run):
            finish_run(run.id, "interrupted", "The run stopped without finishing (the app or the Mac "
                                              "restarted, or it crashed)")
            fixed["runs"] += 1
    for item in _orphans():
        # Only from the state it was read in: one cancelled meanwhile stays cancelled.
        try:
            if item.state in (VALIDATING, PREPARING, READY):
                move(item.id, QUEUED, note="Back in the queue: the run stopped before its form opened, "
                                           "and nothing was sent.", expect=item.state)
                fixed["requeued"] += 1
            else:
                move(item.id, PENDING, action="The app stopped while its form was open, so it may have "
                                              "been sent. " + CHECK_IF_SENT, expect=item.state)
                fixed["to_confirm"] += 1
        except QueueError:
            pass
    return fixed


def _orphans() -> list[ApplyItem]:
    """Jobs on their way whose run is over, or that have none. Read with the runs in one query: a run
    that started a moment ago, after recover looked at the runs, is live in this same read, and its
    jobs are left to it (the Auto-apply page recovers every few seconds while a run starts)."""
    live = select(ApplyRun.id).where(ApplyRun.state.in_(("running", "pausing")))
    with get_session() as session:
        items = (session.query(ApplyItem)
                 .filter(ApplyItem.state.in_(ON_ITS_WAY),
                         or_(ApplyItem.run_id.is_(None), ApplyItem.run_id.notin_(live))).all())
        for item in items:
            session.expunge(item)
    return items


# -- what the Auto-apply page shows, and follows as it happens -------------------------------------
#
# Phase 2 (25 Oct 2026). Every event has an id that only grows. The page is sent each one as it is
# written (`stream`, server-sent events) rather than only a fresh list every few seconds; a page
# that reconnects -- a refresh, a dropped connection, the app restarting, the laptop reopened -- says
# the last id it had and is sent the rest, none twice. Each entry the events changed comes with
# them, as it stands now, carrying `version`, the id of its latest event: a page never puts an
# older picture of an entry over a newer one, whichever way the two reached it.

def _clock(when: datetime, seconds: bool = False) -> str:
    local = when.astimezone()
    text = f"{local.hour % 12 or 12}:{local.minute:02d}" + (f":{local.second:02d}" if seconds else "")
    return f"{text} {'AM' if local.hour < 12 else 'PM'}"


def _utc(when: datetime | None) -> str:
    return when.replace(tzinfo=timezone.utc).isoformat(timespec="seconds") if when else ""


def _event_dict(event: ApplyEvent, job: Job | None) -> dict:
    """One event for the page: what kind -- the run's own, a move to a state, or a step of what is
    happening -- for which job, when (UTC, and the person's own clock), and the words."""
    return {"id": event.id, "item_id": event.item_id, "job_id": event.job_id, "run_id": event.run_id,
            "kind": "run" if event.item_id is None else "state" if event.state else "step",
            "state": event.state or "", "label": LABELS.get(event.state or "", ""), "message": event.message,
            "at": _utc(event.at), "clock": _clock(event.at.replace(tzinfo=timezone.utc), seconds=True),
            "job": f"{job.company} — {job.title}" if job else ""}


def last_event_id() -> int:
    with get_session() as session:
        return session.query(func.max(ApplyEvent.id)).scalar() or 0


def events_after(after: int = 0, *, limit: int = 200, item_id: int | None = None) -> list[dict]:
    """The events written after `after`, oldest first; given `item_id`, only that entry's."""
    with get_session() as session:
        query = (session.query(ApplyEvent, Job).outerjoin(Job, Job.id == ApplyEvent.job_id)
                 .filter(ApplyEvent.id > after))
        if item_id is not None:
            query = query.filter(ApplyEvent.item_id == item_id)
        return [_event_dict(e, j) for e, j in query.order_by(ApplyEvent.id).limit(limit)]


def _latest_events(session, item_ids) -> dict[int, ApplyEvent]:
    """Each entry's latest event."""
    ids = [i for i in item_ids if i]
    if not ids:
        return {}
    newest = (select(func.max(ApplyEvent.id)).where(ApplyEvent.item_id.in_(ids))
              .group_by(ApplyEvent.item_id).scalar_subquery())
    return {e.item_id: e for e in session.query(ApplyEvent).filter(ApplyEvent.id.in_(newest))}


def _shape(item: ApplyItem, job: Job | None, latest: ApplyEvent | None, run_live: bool,
           docs: list | None = None) -> dict:
    """One entry as the page shows it: its state, what it needs from the person, how it ended, and --
    while it is on its way -- the step it is at now and since when; its decision, and the documents
    made ready for it (names and checksums)."""
    state, moving = item.state, item.state in ON_ITS_WAY
    return {"id": item.id, "job_id": item.job_id, "state": state, "label": LABELS[state],
            "note": item.note or "", "action": item.action or "", "outcome": item.outcome or "",
            "attempts": item.attempts, "title": job.title if job else "", "company": job.company if job else "",
            "board": job.source if job else "", "assisted": _assisted(job),
            "score": None if not job or job.ats_match_score is None else round(job.ats_match_score * 100),
            "updated": _clock(item.updated_at.replace(tzinfo=timezone.utc)), "since": _utc(item.updated_at),
            "version": latest.id if latest else 0,
            "current": latest.message if latest and moving else "",
            "current_at": _utc(latest.at) if latest and moving else "",
            "decision": json.loads(item.decision_detail) if item.decision_detail else None,
            "documents": docs or [],
            "can": {"cancel": state in (QUEUED, VALIDATING, PREPARING, READY),
                    "retry": state in (FAILED, SKIPPED, CANCELLED),
                    "confirm": state == PENDING or (state == WAITING and not run_live)}}


def item_views(item_ids, run_live: bool) -> list[dict]:
    """These entries as they stand now."""
    ids = [i for i in item_ids if i]
    if not ids:
        return []
    with get_session() as session:
        rows = (session.query(ApplyItem, Job).outerjoin(Job, Job.id == ApplyItem.job_id)
                .filter(ApplyItem.id.in_(ids)).all())
        latest, docs = _latest_events(session, ids), _documents_of(session, ids)
        return [_shape(i, j, latest.get(i.id), run_live, docs.get(i.id)) for i, j in rows]


def run_view() -> dict:
    """The run as the page shows it, from the table alone (whether its process lives is `recover`'s
    question): going or pausing, and the latest one -- the table holds one live run at most."""
    with get_session() as session:
        last = session.query(ApplyRun).order_by(ApplyRun.id.desc()).first()
        live = last is not None and last.state in ("running", "pausing")
        return {"live": live, "pausing": live and last is not None and last.state == "pausing",
                "last": None if last is None else {
                    "id": last.id, "state": last.state, "note": last.note or "",
                    "started": _clock(last.started_at.replace(tzinfo=timezone.utc))}}


def view(cfg) -> dict:
    """Every job on the list with its state and what it needs, the run, and the latest activity, with
    the id of the latest event (where the stream takes over). Brings the list up to date first: a
    run that died, a job applied to another way."""
    from backend.apply.chosen import check, migrate_queue
    migrate_queue()
    recover()
    sync_with_jobs()
    run = run_view()
    # Each waiting job's decision as things stand now -- the person may have answered a question,
    # or a posting changed -- kept before the list is read, so the list shows it.
    preview = check(cfg, [j for j, _ in _items_in(QUEUED)])
    turn = {j["id"]: j for j in preview["jobs"]}
    keep_decisions({j: row["decision"] for j, row in turn.items() if row.get("decision")})
    with get_session() as session:
        rows = (session.query(ApplyItem, Job).outerjoin(Job, Job.id == ApplyItem.job_id)
                .filter(ApplyItem.hidden.is_(False)).order_by(ApplyItem.position).all())
        latest, docs = _latest_events(session, [i.id for i, _ in rows]), _documents_of(session, [i.id for i, _ in rows])
        items = [_shape(i, j, latest.get(i.id), run["live"], docs.get(i.id)) for i, j in rows]
        events = (session.query(ApplyEvent, Job).outerjoin(Job, Job.id == ApplyEvent.job_id)
                  .order_by(ApplyEvent.id.desc()).limit(40).all())
        activity = [_event_dict(e, j) for e, j in events]
        last_id = session.query(func.max(ApplyEvent.id)).scalar() or 0
    for item in items:
        if item["state"] == QUEUED and item["job_id"] in turn:
            item["turn"], item["turn_reason"] = turn[item["job_id"]]["state"], turn[item["job_id"]]["reason"]
    from backend.apply import runner
    _, label, free = runner.writer(cfg)
    return {"items": items, "events": activity, "last_event_id": last_id, "next_run": preview["ready"],
            "left_today": preview["left_today"], "details_missing": preview["details_missing"],
            "auto_submit": preview["auto_submit"] and not auto_submit_held(), "run": run,
            # Prepare these: the waiting jobs that can go and have no documents made ready yet.
            "to_prepare": sum(bool(i["state"] == QUEUED and i.get("turn") in ("ready", "later", "answers")
                                   and (not i["documents"] or _redo(i["job_id"]))) for i in items),
            "writer": {"label": label, "free": free}}


def stream(after: int = 0, *, seconds: float = 300, every: float = 0.5, heartbeat: float = 15,
           check_alive: float = 5):
    """Server-sent events for the Auto-apply page (text/event-stream): every event after `after` as
    it is written ("activity", carrying the event's id, which the browser hands back when it
    reconnects), then the entries those events changed, as they stand now ("item"), and the run
    whenever it changes ("run"). While a run is going, whether its process still lives is looked at
    every few seconds: one that died is recovered here, so the page sees its jobs set right within
    seconds, not at the next full load. Ends after `seconds` (the browser comes straight back)."""
    def message(kind: str, data: dict, ident: int | None = None) -> str:
        head = f"id: {ident}\n" if ident is not None else ""
        return f"{head}event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"

    started = last_beat = last_alive = time.monotonic()
    last, shown = after, None
    yield "retry: 2000\n\n"
    while True:
        now = time.monotonic()
        if shown is not None and shown["live"] and now - last_alive >= check_alive:
            last_alive = now
            if current_run() is None:                  # its process is gone: set right now
                recover()
        events = events_after(last)
        for event in events:
            yield message("activity", event, event["id"])
        run = run_view()
        if events:
            last = events[-1]["id"]
            for item in item_views({e["item_id"] for e in events}, run["live"]):
                yield message("item", item)
            last_beat = now
        if run != shown:
            yield message("run", run)
            shown = run
        if now - last_beat >= heartbeat:
            yield ": still here\n\n"                    # a comment: keeps the connection seen alive
            last_beat = now
        if now - started >= seconds:
            return
        time.sleep(every)


# -- the run itself ------------------------------------------------------------------------------

def work(cfg, applicant, *, browser_factory=None, tailor: bool = True, wait_s: float = 900,
         paid_ok: bool = False) -> list:
    """Go down the chosen jobs, one at a time, each checked again just before it goes. Returns what
    became of each, as the other runs do (runner.Outcome). A job whose documents were not made ready
    before has them made on the way, by the writer set in Settings: one that bills per use only once
    the person has said yes (`paid_ok`)."""
    from backend.apply import runner
    run = start_run(os.getpid())
    _, label, free = runner.writer(cfg)
    if not free and not paid_ok:
        why = (f"Not started: a resume or cover letter made on the way would use {label}, which bills per use. "
               "Press Apply to these again and say yes to that, or choose a free writer in Settings.")
        finish_run(run.id, "stopped", why)
        runner.say(why)
        return []
    runner.say(f"Applying to your chosen jobs · "
               f"{'auto-submit where nothing is left for you and the job is Eligible (Greenhouse)' if not applicant.stop_before_submit else 'you press Submit on each'}")
    sync_with_jobs()
    search = runner.searched_places(cfg)
    browser = {"context": None, "close": lambda: None, "factory": browser_factory}   # opened at the first form
    outcomes, why, taken = [], "Done: nothing more on your list.", set()
    try:
        while True:
            heartbeat(run.id)
            if pause_asked(run.id):
                why = "Paused: the rest wait until you press Resume."
                finish_run(run.id, "paused", why)
                break
            item_id, why_none = _next(cfg, applicant, run.id, search)
            if item_id is None:
                why = why_none
                finish_run(run.id, "done" if why.startswith("Done") else "stopped", why)
                break
            # Never twice in one run: a job sent back to wait that came up again would go round for ever.
            if item_id in taken:
                _try(item_id, QUEUED, note="Back in the queue: it came up twice in one run.", expect=VALIDATING)
                why = "Stopped: the same job came up twice in this run, so nothing more was tried."
                finish_run(run.id, "stopped", why)
                break
            taken.add(item_id)
            outcome = _one(cfg, applicant, run.id, item_id, search, browser, tailor=tailor, wait_s=wait_s)
            if outcome is not None:
                outcomes.append(outcome)
                if outcome.result == "stopped":
                    why = "Stopped: the app's browser was closed."
                    finish_run(run.id, "stopped", why)
                    break
    except BaseException as exc:
        finish_run(run.id, "stopped", f"Stopped by an error: {type(exc).__name__}: {str(exc)[:200]}")
        raise
    finally:
        browser["close"]()
    runner.say(why)
    return runner.finish(outcomes)


def _next(cfg, applicant, run_id: int, search) -> tuple[int | None, str]:
    """Take the next job to go, or say why nothing more goes now. A job that cannot go at all leaves
    the queue here, skipped, with its reason."""
    from backend.apply import runner
    while True:
        with get_session() as session:
            queued = session.query(ApplyItem).filter(ApplyItem.state == QUEUED).order_by(ApplyItem.position).all()
            if not queued:
                return None, "Done: nothing more on your list."
            left = runner.remaining_today(cfg, applicant,
                                          runner._applied_since(session, runner.start_of_today_utc()))
            found = runner.jobs_by_id(session, [i.job_id for i in queued])
            gone = [(i.id, None, "this job is no longer in your jobs list") for i in queued if i.job_id not in found]
            gone += [(i.id, found[i.job_id].status, why) for i in queued if i.job_id in found
                     for why in [runner.blocked_reason(cfg, found[i.job_id], applicant, search, any_board=True)] if why]
            out = {item_id for item_id, _, _ in gone}
            # Its posting asks what the profile leaves unanswered: it waits for the answer, on the
            # list, while the rest go (chosen_queue passes it over too).
            asked = {i.id: runner.decision(cfg, found[i.job_id], applicant, search) for i in queued
                     if i.id not in out and runner.needs_answer(found[i.job_id], applicant)}
            from backend.apply import prepare
            entries = prepare.load()
            answering = {i.id for i in queued if i.id not in out and i.id not in asked
                         and runner.questions_waiting(found[i.job_id], applicant, entries)}
            going = [] if left <= 0 else runner.chosen_queue(
                cfg, applicant, session, [i.job_id for i in queued if i.job_id in found], 1, search=search)
            pick = next((i.id for i in queued if going and i.job_id == going[0].job_id), None)
            rest = len(queued) - len(out) - len(asked) - len(answering)
        for item_id, verdict in asked.items():
            decided(item_id, verdict, run_id=run_id)           # written down once, not at every look
        for item_id, status, why in gone:
            if status in APPLIED_STATUSES:                   # applied to another way since it was added
                _try(item_id, APPLIED, outcome=_elsewhere(status), expect=QUEUED)
                continue
            try:
                move(item_id, VALIDATING, run_id=run_id, note="Checking it can still go", expect=QUEUED)
                move(item_id, SKIPPED, outcome=f"Can't go: {why}.", expect=VALIDATING)
            except QueueError:
                pass
        if left <= 0:
            return None, "Today's limit is reached: the rest wait for tomorrow."
        if pick is None:
            waits = "; ".join(w for w in (
                f"{len(asked)} wait for your answer (each says what it needs)" if asked else "",
                f"{len(answering)} wait for your answers to their forms' questions (Questions page)" if answering else "")
                if w)
            if waits and not rest:
                return None, f"Nothing more can go now: {waits}."
            return None, ("Nothing more can go now: " + (f"{waits}; " if waits else "")
                          + "the rest wait (one a company, 12 hours apart).")
        try:
            move(pick, VALIDATING, run_id=run_id, expect=QUEUED, note="Checking it can still go")
            return pick, ""
        except QueueError:
            continue                                         # taken or cancelled meanwhile: look again


def _one(cfg, applicant, run_id: int, item_id: int, search, browser, *, tailor: bool, wait_s: float):
    """One job, from validating to how it ended."""
    from backend.apply import runner
    with get_session() as session:
        item = session.get(ApplyItem, item_id)
        job = session.get(Job, item.job_id) if item is not None else None
        if job is not None:
            session.expunge(job)
    if item is None:
        return None
    if job is None:                                          # deleted meanwhile (a prune, say)
        _try(item_id, SKIPPED, outcome="Can't go: this job is no longer in your jobs list.", expect=VALIDATING)
        return None
    head = f"[{job.company} — {job.title}]"

    # Validating: checked again now, just before it goes, and its decision written down.
    if _checked(cfg, applicant, run_id, item_id, job, search, expect=VALIDATING) is None:
        return None
    try:
        move(item_id, PREPARING, note="Making the resume and cover letter ready (a tailoring takes a few minutes)",
             expect=VALIDATING)
    except QueueError:
        return None                                          # cancelled meanwhile
    told, say = runner.say, _steps(item_id, run_id, head)

    # Preparing: the documents made ready for it before (Prepare these) go as they are, checked when
    # its form opens; else they are made now. Either way each file is written down first.
    made = None
    earlier = documents(item_id)
    redo = _redo(job.id) if earlier else None
    if redo:                                                 # never sent as it is (runner.choose_resume)
        _gone_stale(item_id)
        note(item_id, f"Made ready before, but its resume is to be made again first: the one made for it was {redo}",
             run_id=run_id)
        earlier = []
    if earlier:
        resume_note = next((d["source"] for d in earlier if d["kind"] == "resume"), "")
        note(item_id, "Using the documents made ready for it on " + earlier[0]["at"][:10] + ": "
             + ", ".join(d["name"] for d in earlier), run_id=run_id)
    else:
        runner.say = say
        try:
            made = runner.prepare_documents(cfg, job, applicant, head, tailor=tailor)
        finally:
            runner.say = told
        if isinstance(made, str):
            _try(item_id, FAILED, outcome=f"Not sent: {made}. Retry puts it back in the queue.", expect=PREPARING)
            return runner.Outcome(job.id, job.company, job.title, "not-filled", detail=made)
        record_documents(item_id, made, run_id=run_id)
        resume_note = made.resume_note
        waiting = _questions(applicant, item_id, job, run_id)
        if waiting:                                      # answered first, then its form: the rest go on
            _try(item_id, QUEUED, note=_waits_for_answers(waiting), expect=PREPARING)
            return None
    try:
        move(item_id, READY, note=f"Ready ({resume_note}): the form opens next", expect=PREPARING)
    except QueueError:
        return None                                          # cancelled while it was being prepared
    if pause_asked(run_id):
        _try(item_id, QUEUED, note="Paused before its form opened: ready to go when you resume.", expect=READY)
        return None

    # Checked once more: tailoring takes minutes, and the job may have been applied to meanwhile.
    now = _job(job.id) or job
    verdict = _checked(cfg, applicant, run_id, item_id, now, search, expect=READY)
    if verdict is None:
        return None
    # Sent by the app itself only when nothing is in doubt, and only on Greenhouse's own board; else the
    # person presses Submit. Never on another board, even one whose posting leads to a Greenhouse form
    # (assisted, 26 Oct 2026). Said only where it could have sent itself: Workday and JazzHR always wait.
    auto = not applicant.stop_before_submit and verdict.sends_itself and job.source == "greenhouse"
    if runner.assisted(job):
        note(item_id, runner.assisted(job), run_id=run_id)
    if auto and auto_submit_held():
        auto = False
        note(item_id, "You press Submit on this one: automatic Submit is held until the latest safety fixes "
                      "are approved", run_id=run_id)
    from backend.apply import prepare
    if auto and prepare.questions_unread(job.id):
        # Its form's questions were not read: read them again now. Read, the mark goes, and any required
        # one still unanswered sends it back to wait; still not read, the person presses Submit.
        waiting = _questions(applicant, item_id, job, run_id)
        if waiting:
            _try(item_id, QUEUED, note=_waits_for_answers(waiting), expect=READY)
            return None
        unread = prepare.questions_unread(job.id)
        if unread:
            auto = False
            note(item_id, f"You press Submit on this one: its form's questions could not be read ahead ({unread})",
                 run_id=run_id)
    elif not applicant.stop_before_submit and not auto and job.source == "greenhouse":
        note(item_id, f"You press Submit on this one: it is {verdict.label.lower()} ({'; '.join(verdict.reasons)})",
             run_id=run_id)

    # Running.
    try:
        move(item_id, RUNNING, note="Opening the form and filling it", expect=READY)
    except QueueError:
        return None                                          # cancelled just now: its form is never opened
    # Exactly the documents written down: copied into this attempt's own folder, and checked there
    # against their checksums, before the form is given them.
    from backend.apply.greenhouse import _evidence_dir
    evidence = made.evidence if made is not None else _evidence_dir(job.id)
    sealed = seal(item_id, evidence)
    if isinstance(sealed, str):
        _try(item_id, FAILED, outcome=f"Not sent: {sealed}. Retry makes its documents ready again.", expect=RUNNING)
        return runner.Outcome(job.id, job.company, job.title, "not-filled", detail=sealed)
    ready = runner.Prepared(sealed[0], sealed[1], sealed[2], evidence)
    link = _link(item_id, sent={"resume": sealed[0], "cover_letter": sealed[1]})
    note(item_id, "Checked before the form opens: " + ", ".join(
        f"{p.name} (checksum {_sha256(p)[:12]})" for p in sealed[:2] if p is not None), run_id=run_id)
    unready = documents_unready(job.id, sealed[0], sealed[1]) if auto else None
    if unready:
        auto = False
        note(item_id, f"You press Submit on this one: {unready}", run_id=run_id)
    if browser["context"] is None:
        browser["context"], browser["close"] = runner.open_browser(cfg, browser.get("factory"))
    try:
        page = browser["context"].new_page()
    except Exception:
        _try(item_id, QUEUED, note="Back in the queue: the app's browser was closed before its form opened.",
             expect=RUNNING)
        return runner.Outcome(job.id, job.company, job.title, "stopped")
    runner.say = say
    try:
        with runner.answers_for(applicant, job), runner.linked(link):
            outcome = runner.fill_form(cfg, page, job, applicant, head, ready, auto=auto, wait_s=wait_s)
    except Exception as exc:
        _try(item_id, PENDING, action="The app hit an error while its form was open, so it may have been sent. "
                                      + CHECK_IF_SENT, expect=FORM_OPEN)
        runner.say = told
        runner.close_page(page)
        raise exc
    finally:
        runner.say = told
    runner.close_page(page)
    _settle(item_id, outcome, wait_s)
    return outcome


def _checked(cfg, applicant, run_id: int, item_id: int, job, search, *, expect: str, questions: bool = True):
    """Checked again now, and its decision written down (always at validating; at a later check only
    when it changed). The decision -- or None when the job cannot go (it leaves the queue with the
    reason) or waits for the person's answer (it goes back to wait, answered "don't know" since), or,
    with `questions`, for their answers to its form's questions (Prepare these makes it ready anyway)."""
    from backend.apply import runner
    why = runner.blocked_reason(cfg, job, applicant, search, any_board=True)
    if why:
        _cannot_go(item_id, job, why, expect=expect)
        return None
    verdict = runner.decision(cfg, job, applicant, search)
    decided(item_id, verdict, run_id=run_id, always=expect == VALIDATING)
    if not verdict.goes:
        _try(item_id, QUEUED, note=f"Waits for your answer: {'; '.join(verdict.unknowns)}", expect=expect)
        return None
    waiting = runner.questions_waiting(job, applicant) if questions else []
    if waiting:
        _try(item_id, QUEUED, note=_waits_for_answers(waiting), expect=expect)
        return None
    return verdict


def _waits_for_answers(waiting: list[dict]) -> str:
    shown = ", ".join(f"“{q['label'][:70]}”" for q in waiting[:2]) + (" …" if len(waiting) > 2 else "")
    return (f"Waits for your answers: {len(waiting)} required question{'s' if len(waiting) != 1 else ''} on its "
            f"form, on the Questions page ({shown})")


def _questions(applicant, item_id: int, job, run_id: int) -> list[dict]:
    """The questions its form asks, read from the board now (prepare.open_questions: Greenhouse's and
    JazzHR's; another form's are seen only on the form) and kept with the job's answers, for the
    Questions page; the required ones nothing answers yet. Never answered by a guess."""
    from backend.apply import prepare, runner
    before, applicant.job = applicant.job, job          # a salary question answered by the person's rule
    try:
        asked = prepare.open_questions(job, applicant)
    except Exception as exc:                             # not read: kept as such, never as "none asked"
        why = str(exc) if isinstance(exc, prepare.QuestionsUnread) else type(exc).__name__
        prepare.mark_questions_unread(job, why)
        note(item_id, f"Its form's questions could not be read ahead ({why}): it goes on, and you press "
                      "Submit on it", run_id=run_id)
        return []
    finally:
        applicant.job = before
    if asked or str(job.id) in prepare.load():
        prepare.keep_questions(job, asked)
    waiting = runner.questions_waiting(job, applicant)
    if asked:
        note(item_id, f"Its form asks {len(asked)} question{'s' if len(asked) != 1 else ''} nothing answers yet: "
                      + (f"{len(waiting)} required, for you on the Questions page" if waiting else "none required"),
             run_id=run_id)
    return waiting


def _steps(item_id: int, run_id: int, head: str):
    """Every line the run writes about this job, as a step on the page (Phase 2), from its documents on:
    until 26 Oct 2026 only the form's were, and a tailoring of several minutes showed no step at all."""
    from backend.apply import runner
    told = runner.say

    def say(line: str) -> None:
        told(line)
        words = line.strip().removeprefix(f"{head}: ")          # the page shows whose step it is
        note(item_id, words, run_id=run_id)                      # every line kept, in the activity list
        if "Waiting for you" in words:
            asked = words.removeprefix("Waiting for you — ")
            if not _try(item_id, WAITING, action=asked, expect=RUNNING):
                _ask_again(item_id, asked)                       # a further request: it replaces the one shown
    return say


# -- Prepare these (26 Oct 2026, phase 3) ----------------------------------------------------------

def prepare_these(cfg, applicant, *, tailor: bool = True, paid_ok: bool = False) -> int:
    """Each chosen job waiting on the list that can go has its resume (tailored where it can be; one
    tailored and approved before is used as it is) and cover letter made ready now, and written down --
    each file and its checksum -- with no form opened and nothing sent. Apply to these then gives each
    form exactly those files. Best fit first; a job made ready already is not made again; one whose
    posting asks what the profile leaves unanswered waits; one that cannot go leaves the list with
    the reason, as on a run. One run at a time: never beside Apply to these. Returns how many."""
    from backend.apply import runner
    run = start_run(os.getpid())
    _, label, free = runner.writer(cfg)
    if not free and not paid_ok:                             # cover letters use it too, tailoring or not
        why = (f"Not prepared: tailoring and cover letters would use {label}, which bills per use. Press "
               "Prepare these again and say yes to that, or choose a free writer in Settings.")
        finish_run(run.id, "stopped", why)
        runner.say(why)
        return 0
    runner.say(f"Making your chosen jobs ready: resume and cover letter for each, nothing sent · writer: {label}")
    search = runner.searched_places(cfg)
    made, taken = 0, set()
    try:
        while True:
            heartbeat(run.id)
            if pause_asked(run.id):
                why = f"Paused: {made} made ready; the rest when you press Prepare these again."
                finish_run(run.id, "paused", why)
                break
            item_id = _next_to_prepare(cfg, applicant, run.id, search, taken)
            if item_id is None:
                why = f"Done: {made} made ready, nothing sent. Apply to these gives each form exactly those files."
                finish_run(run.id, "done", why)
                break
            taken.add(item_id)
            made += _prepare(cfg, applicant, run.id, item_id, search, tailor=tailor)
    except BaseException as exc:
        finish_run(run.id, "stopped", f"Stopped by an error: {type(exc).__name__}: {str(exc)[:200]}")
        raise
    runner.say(why)
    return made


def _next_to_prepare(cfg, applicant, run_id: int, search, taken: set) -> int | None:
    """The next job to make ready -- best fit first, not made ready already -- taken (validating); or None."""
    from backend.apply import runner
    with get_session() as session:
        queued = session.query(ApplyItem).filter(ApplyItem.state == QUEUED).order_by(ApplyItem.position).all()
        have = {i for (i,) in session.query(ApplyArtifact.item_id).filter(ApplyArtifact.state == "ready",
                                                                          ApplyArtifact.kind == "resume")}
        have -= {i.id for i in queued if i.id in have and _redo(i.job_id)}    # made again: see _one
        found = runner.jobs_by_id(session, [i.job_id for i in queued])
        waiting = sorted((i for i in queued if i.id not in taken and i.id not in have),
                         key=lambda i: -(found[i.job_id].ats_match_score or 0) if i.job_id in found else 0)
        waiting = [(i.id, found.get(i.job_id)) for i in waiting]
    for item_id, job in waiting:
        if job is None:
            _gone_from_jobs(item_id, run_id)
            continue
        why = runner.blocked_reason(cfg, job, applicant, search, any_board=True)
        if why is None and runner.needs_answer(job, applicant):
            decided(item_id, runner.decision(cfg, job, applicant, search), run_id=run_id)   # waits; said once
            continue
        if why is not None and job.status in APPLIED_STATUSES:
            _try(item_id, APPLIED, outcome=_elsewhere(job.status), expect=QUEUED)
            continue
        try:
            move(item_id, VALIDATING, run_id=run_id, note="Checking it can still go", expect=QUEUED)
        except QueueError:
            continue                                         # taken or cancelled meanwhile
        if why is not None:
            _try(item_id, SKIPPED, outcome=f"Can't go: {why}.", expect=VALIDATING)
            continue
        return item_id
    return None


def _gone_from_jobs(item_id: int, run_id: int) -> None:
    try:
        move(item_id, VALIDATING, run_id=run_id, note="Checking it can still go", expect=QUEUED)
        move(item_id, SKIPPED, outcome="Can't go: this job is no longer in your jobs list.", expect=VALIDATING)
    except QueueError:
        pass


def _prepare(cfg, applicant, run_id: int, item_id: int, search, *, tailor: bool) -> int:
    """One job made ready, from validating back to waiting its turn with its documents written down: 1,
    or 0 when it cannot go, waits for an answer, or its documents could not be made."""
    from backend.apply import runner
    with get_session() as session:
        item = session.get(ApplyItem, item_id)
        job = session.get(Job, item.job_id) if item is not None else None
        if job is not None:
            session.expunge(job)
    if job is None:
        _try(item_id, SKIPPED, outcome="Can't go: this job is no longer in your jobs list.", expect=VALIDATING)
        return 0
    head = f"[{job.company} — {job.title}]"
    if _checked(cfg, applicant, run_id, item_id, job, search, expect=VALIDATING, questions=False) is None:
        return 0
    try:
        move(item_id, PREPARING, note="Making the resume and cover letter ready (a tailoring takes a few minutes)",
             expect=VALIDATING)
    except QueueError:
        return 0                                             # cancelled meanwhile
    told = runner.say
    runner.say = _steps(item_id, run_id, head)
    try:
        made = runner.prepare_documents(cfg, job, applicant, head, tailor=tailor)
    finally:
        runner.say = told
    if isinstance(made, str):
        _try(item_id, FAILED, outcome=f"Not made ready: {made}. Retry puts it back in the queue.", expect=PREPARING)
        return 0
    record_documents(item_id, made, run_id=run_id)
    _questions(applicant, item_id, job, run_id)        # when it goes, its turn says (answers awaited, ...)
    _try(item_id, QUEUED, note=f"Made ready ({made.resume_note}): exactly these files go with its form",
         expect=PREPARING)
    return 1


@_when_free
def _ask_again(item_id: int, action: str) -> bool:
    """A further request from a form already waiting for the person -- the CAPTCHA done, now Submit:
    it replaces the one the list shows (25 Oct 2026; the first one stayed until the form ended). Only
    while it waits: a form settled meanwhile keeps its ending. The activity list has every request."""
    with get_session() as session:
        return bool(session.query(ApplyItem).filter(ApplyItem.id == item_id, ApplyItem.state == WAITING)
                    .update({"action": action, "updated_at": _plain(_now())}, synchronize_session=False))


def _job(job_id: int):
    """The job as the database has it now."""
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is not None:
            session.expunge(job)
        return job


def _try(item_id: int, to: str, **fields) -> bool:
    """A move another process may have beaten: False when it did. A move MOVES does not allow is
    not swallowed: it is a mistake in this module, and must not go unseen (a job once sat in
    "ready" for good that way). Each one names the state the run left the job in (`expect`), so
    a move made meanwhile by someone else -- the person's word, a recovery -- is the one that stands."""
    try:
        move(item_id, to, **fields)
        return True
    except Conflict:
        return False


def _cannot_go(item_id: int, job, why: str, *, expect: str) -> None:
    """A job that cannot go after all leaves the queue with its reason; one applied to another way
    meanwhile (its own page, an employer's email) is Applied here too, as sync_with_jobs has it."""
    if job.status in APPLIED_STATUSES:
        _try(item_id, APPLIED, outcome=_elsewhere(job.status), expect=expect)
    else:
        _try(item_id, SKIPPED, outcome=f"Can't go: {why}.", expect=expect)


def _settle(item_id: int, outcome, wait_s: float) -> None:
    """Where a form's ending leaves its job. Only a confirmation page is Applied; no answer in time
    may still have gone, so it waits for the person's word rather than being tried again."""
    result = outcome.result
    if result == "submitted":
        how = "sent automatically" if outcome.how == "auto" else "you submitted it"
        if _try(item_id, APPLIED, outcome=f"Applied ✓ ({how}, {_clock(_now())}).", expect=FORM_OPEN):
            _sent(item_id)
    elif result == "skipped":
        _try(item_id, SKIPPED, outcome="You closed its form: nothing was sent. Retry puts it back in the queue.",
             expect=FORM_OPEN)
    elif result == "timeout":
        _try(item_id, PENDING, action=f"No confirmation was seen in {wait_s / 60:g} minutes, and it may "
                                      "have gone. " + CHECK_IF_SENT, expect=FORM_OPEN)
    elif result == "handoff":
        _try(item_id, PENDING, action="Send it from your own Chrome (its page lists every answer), then say "
                                      "whether it went.", expect=FORM_OPEN)
    elif result == "stopped":
        _try(item_id, QUEUED, note="Back in the queue: the app's browser was closed.", expect=FORM_OPEN)
    else:
        _try(item_id, FAILED, outcome=f"Not sent: {outcome.detail or 'the form could not be filled'}. "
                                      "Retry puts it back in the queue.", expect=FORM_OPEN)
