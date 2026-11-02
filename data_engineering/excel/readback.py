"""Status changes read back from the Excel sheet into the app (Phase 9, 31 Oct 2026).

The DB stays the source of truth and the sheet a view (README §C7), but the sheet's Application
Status column is the person's to edit, and until now an edit there never reached the app. This
reads those edits back -- only those, and only when it is safe:

- Only a cell changed in the sheet since the last read-back counts. What each row showed, and the
  app's status beside it, are kept next to the workbook (<workbook>.readback.json), and the export
  notes there each status it writes itself (`kept_in_step`), so the app's own writing is never
  taken for the person's and a sheet that is merely behind never turns the app back.
- Only the person's own statuses come in: Skipped, Applied, Interviewing, Offer, Rejected. Not
  Applied, Manual Review and Closed are the app's own (SYSTEM_STATUSES): set those on the job page.
- An application stays an application: never back to Skipped, never back a stage (Interviewing to
  Applied); Offer and Rejected are final.
- A job whose status the app changed by a decision since the last read is "changed in both
  places": the app's status stands. On a first read, with nothing kept, the sheet is taken only
  over a status the app set by itself, and never for a job on record as applied.
- A job marked applied this way gets its history line, "marked" on "the Excel sheet", once, as on
  the job page (runner.record_marked).
- Each change is one conditional update: a job whose status moved while this ran keeps the new one.

Preview by default: nothing is written, and the workbook never is.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import or_

from data_engineering.db.models import DEFAULT_STATUS, STATUS_VALUES, SYSTEM_STATUSES, Job
from data_engineering.db.session import get_session
from data_engineering.excel.tracker import SHEET_NAME, _normalise_header, job_key

TAKE, SAME, UNCHANGED, APPS_OWN, NOT_IN_APP = "take", "same", "unchanged", "app's own", "not in the app"
BOTH, REFUSED, NOT_A_STATUS, TWO_ROWS, MOVED = "both", "refused", "not a status", "two rows", "moved"
NOT_TAKEN = (BOTH, REFUSED, NOT_A_STATUS, TWO_ROWS, MOVED)

# How far an application has got (backend.apply.runner.APPLIED_STATUSES): it only ever moves on.
STAGE = {"Applied": 0, "Interviewing": 1, "Offer": 2, "Rejected": 2}

_SPELLING = {status.lower(): status for status in STATUS_VALUES}


class Unreadable(Exception):
    """The sheet cannot be read safely; nothing was changed."""


@dataclass
class Change:
    key: str
    job_id: int | None
    job: str            # "Company — Title", for the report
    sheet: str          # what the sheet shows: a status in its own spelling, or the text as typed
    app: str            # the job's status in the app when it was read
    verdict: str = ""
    why: str = ""


def seen_path(workbook: Path) -> Path:
    workbook = Path(workbook)
    return workbook.with_name(workbook.stem + ".readback.json")


def _load_seen(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_seen(path: Path, seen: dict) -> None:
    from backend.apply.filesafe import write_whole
    write_whole(path, json.dumps(seen, sort_keys=True, separators=(",", ":")))


@contextmanager
def kept_in_step(workbook: Path):
    """Held by the export while it writes the sheet: it notes each status it writes itself, so a
    read-back never takes the app's writing for the person's edit. None -- nothing kept, nothing
    locked -- until a read-back has run."""
    path = seen_path(workbook)
    if not path.exists():
        yield None
        return
    from backend.apply.filesafe import one_writer
    with one_writer(path):
        seen = _load_seen(path)
        yield seen
        _save_seen(path, seen)


def _text(value: object) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _status(text: str) -> str:
    """A status in its own spelling, however it was typed; the text itself when it is none."""
    return _SPELLING.get(text.lower(), text)


def _read_sheet(workbook: Path) -> dict[str, list[str]]:
    """Each Job Key in the sheet -> the status every row of it shows. Blank cells and rows typed by
    hand without a key are left out: neither says anything about a job the app has."""
    if not workbook.exists():
        raise Unreadable(f"No sheet at {workbook} yet: run `main.py export` first.")
    try:
        wb = load_workbook(workbook, read_only=True, data_only=True)
    except Exception as exc:         # half saved, or not a workbook: read nothing, change nothing
        raise Unreadable(f"Could not read {workbook.name} ({type(exc).__name__}): nothing was changed.") from exc
    try:
        if SHEET_NAME not in wb.sheetnames:
            raise Unreadable(f"{workbook.name} has no {SHEET_NAME} sheet: nothing was changed.")
        rows = wb[SHEET_NAME].iter_rows(values_only=True)
        wanted = {_normalise_header(name): name for name in ("Job Key", "Application Status")}
        at: dict[str, int] = {}
        for col, value in enumerate(next(rows, ())):
            name = wanted.get(_normalise_header(value))
            if name and name not in at:      # a duplicate header loses, as in the export
                at[name] = col
        if len(at) < 2:
            raise Unreadable(f"{workbook.name} has no Job Key or Application Status column: nothing was changed.")
        found: dict[str, list[str]] = {}
        for row in rows:
            key, status = (_text(row[at[name]]) if at[name] < len(row) else ""
                           for name in ("Job Key", "Application Status"))
            if key and status:
                found.setdefault(key, []).append(status)
        return found
    finally:
        wb.close()


def _judge(change: Change, shown: set[str], before: dict | None, on_record: set[int]) -> tuple[str, str]:
    sheet, app = change.sheet, change.app
    if len(shown) > 1:
        return TWO_ROWS, "the sheet has rows for this job that say " + " and ".join(sorted(shown))
    if sheet == app:
        return SAME, ""
    if before and sheet == before.get("sheet"):
        return UNCHANGED, ""
    if sheet not in STATUS_VALUES:
        return NOT_A_STATUS, f"{sheet!r} is not a status"
    if sheet in SYSTEM_STATUSES:
        return APPS_OWN, ""
    then = before.get("app") if before else None
    if then is not None and app != then and not (app in SYSTEM_STATUSES and then in SYSTEM_STATUSES):
        return BOTH, f"changed in both places since the last read: the app now has {app}"
    if then is None and app not in SYSTEM_STATUSES:
        return BOTH, f"the app already has {app}, and nothing read before tells which came last"
    if then is None and change.job_id in on_record:
        return BOTH, f"on record as applied, and set back to {app} in the app"
    if app in STAGE:
        if sheet not in STAGE:
            return REFUSED, "an application stays an application"
        if STAGE[sheet] <= STAGE[app]:
            return REFUSED, "Offer and Rejected are final" if STAGE[app] == 2 else "a stage never goes back"
    return TAKE, ""


def plan(workbook: Path) -> list[Change]:
    """What reading the sheet back would do, row by row. Reads only."""
    from backend.apply import tracker as applications
    from data_engineering.db.models import ApplyHistoryOutbox

    workbook = Path(workbook)
    shown = _read_sheet(workbook)
    seen = _load_seen(seen_path(workbook))
    with get_session() as session:
        jobs = {job_key(row): row for row in session.query(
            Job.id, Job.source, Job.company_slug, Job.external_id, Job.company, Job.title, Job.status)}
        on_record = set(applications._audit()) | {
            job_id for (job_id,) in session.query(ApplyHistoryOutbox.job_id).distinct()}
    changes = []
    for key, values in shown.items():
        job = jobs.get(key)
        sheet = _status(values[0])
        if job is None:
            changes.append(Change(key, None, key, sheet, "", NOT_IN_APP))
            continue
        change = Change(key, job.id, f"{job.company} — {job.title}", sheet, job.status or DEFAULT_STATUS)
        before = seen.get(key)
        change.verdict, change.why = _judge(change, {_status(v) for v in values},
                                            before if isinstance(before, dict) else None, on_record)
        changes.append(change)
    return changes


def _take(workbook: Path, changes: list[Change]) -> list[Change]:
    from backend.apply.history_outbox import export_pending
    from backend.apply.runner import APPLIED_STATUSES, record_marked

    marked = []
    with get_session() as session:
        for change in changes:
            if change.verdict != TAKE:
                continue
            still = [Job.status == change.app]
            if change.app == DEFAULT_STATUS:
                still += [Job.status.is_(None), Job.status == ""]
            if not session.query(Job).filter(Job.id == change.job_id, or_(*still)).update(
                    {"status": change.sheet, "exported_to_excel": False}, synchronize_session=False):
                change.verdict, change.why = MOVED, "its status changed in the app while this ran; the app's stands"
                continue
            if change.sheet in APPLIED_STATUSES and record_marked(
                    session, session.get(Job, change.job_id), change.sheet, "the Excel sheet"):
                marked.append(change.job_id)
    for job_id in marked:
        export_pending(job_id)
    # What each row showed and what the app has now: the next read takes only what changes after this.
    _save_seen(seen_path(workbook), {
        c.key: {"sheet": c.sheet, "app": c.sheet if c.verdict == TAKE else c.app}
        for c in changes if c.job_id is not None and c.verdict not in (TWO_ROWS, MOVED)})
    return changes


def read_back(workbook: Path, apply: bool = False) -> list[Change]:
    """The sheet's status changes, judged; with apply=True, taken in. One read-back at a time, and
    never while an export is writing what it notes (`kept_in_step`)."""
    workbook = Path(workbook)
    if not apply:
        return plan(workbook)
    from backend.apply.filesafe import one_writer
    with one_writer(seen_path(workbook)):
        return _take(workbook, plan(workbook))


def report(changes: list[Change], applied: bool) -> list[str]:
    taken = [c for c in changes if c.verdict == TAKE]
    left = [c for c in changes if c.verdict in NOT_TAKEN]
    gone = sum(c.verdict == NOT_IN_APP for c in changes)
    lines = [f"{'Taken in' if applied else 'To take in'}: {len(taken)}"]
    lines += [f"  {c.job}: {c.app} → {c.sheet}" for c in taken]
    lines.append(f"Not taken: {len(left)}")
    lines += [f"  {c.job}: the sheet says {c.sheet}, the app {c.app} — {c.why}" for c in left]
    lines.append(f"Nothing to take: {len(changes) - len(taken) - len(left) - gone} (the same as the app, not "
                 "changed since the last read, or one of the app's own statuses, which are set on the job page)")
    if gone:
        lines.append(f"Not in the app: {gone} rows for jobs the app no longer keeps")
    if not applied:
        lines.append("Preview: nothing was written. Add --apply to take these in.")
    return lines
