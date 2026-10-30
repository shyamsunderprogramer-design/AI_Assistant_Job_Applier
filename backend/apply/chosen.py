"""The jobs the person chose themselves: ticked on the Jobs page, then added to Auto-apply.

  selected  the jobs ticked on the Jobs page, kept in data/apply/chosen.json. They stay ticked
            through filters, reloads and other pages until the person unticks them or presses
            Clear. Adding them to Auto-apply moves the ones that can go and leaves ticked, with
            the reason, the ones that cannot.
  the queue the jobs added to Auto-apply ("Your chosen jobs" on the Auto-apply page), kept in the
            database since 24 Oct 2026 (backend/apply/execution.py) so a crash or a restart never
            loses where each stands. They are applied to only when the person presses Apply to
            these there. (Until that day the queue sat in this file too; migrate_queue moves any
            left here into the database, once.)

Ticking a job or adding it sends nothing (24 Oct 2026: "No job application is submitted merely
because I selected a job"). Each job is checked by the rules every run keeps
(runner.blocked_reason): open, not applied to or marked by the person, a link to apply through,
the profile allows it, in a place the search names, no older than apply.max_age_months. Any
board goes (26 Oct 2026): one with no filler of its own is assisted -- the app fills what it can
and the person sends it (runner.assisted). Two more rules decide only when it goes, not whether: one job a company,
twelve hours apart, and today's limit. The jobs chosen are never swapped for the app's own picks.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

from backend.config.loader import PROJECT_ROOT

PATH = PROJECT_ROOT / "data" / "apply" / "chosen.json"
_lock = threading.Lock()

READY, LATER, NEEDS, ANSWERS, LISTED, BLOCKED = "ready", "later", "needs", "answers", "listed", "blocked"
_RANK = {READY: 0, LATER: 1, NEEDS: 2, ANSWERS: 3, LISTED: 4, BLOCKED: 5}
STATES = tuple(_RANK)
MAX_IDS = 20_000            # more than every open job; a bigger request is a mistake, not a choice


class NotChosen(ValueError):
    """A request the lists cannot take, with a reason the person can act on."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["selected"] = [i for i in dict.fromkeys(data.get("selected") or []) if _is_id(i)]
    queue, seen = [], set()
    for entry in data.get("queue") or []:
        if isinstance(entry, dict) and _is_id(entry.get("job_id")) and entry["job_id"] not in seen:
            seen.add(entry["job_id"])
            queue.append(entry)
    data["queue"] = queue
    return data


def _save(data: dict) -> None:
    data["updated"] = _now()
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(PATH)


def ids(values) -> list[int]:
    """Job numbers from a request: whole numbers only, each once, in the order given."""
    if not isinstance(values, list):
        raise NotChosen("Send the job numbers as a list.")
    if len(values) > MAX_IDS:
        raise NotChosen(f"That is {len(values):,} jobs; at most {MAX_IDS:,} can be chosen at once.")
    bad = [v for v in values if not _is_id(v)]
    if bad:
        raise NotChosen(f"{bad[0]!r} is not a job number.")
    return list(dict.fromkeys(values))


def _known(job_ids: list[int]) -> set[int]:
    """Which of these are jobs in the database."""
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session
    found: set[int] = set()
    with get_session() as session:
        for n in range(0, len(job_ids), 500):
            found.update(i for (i,) in session.query(Job.id).filter(Job.id.in_(job_ids[n:n + 500])))
    return found


# -- the jobs ticked on the Jobs page --------------------------------------------------------

def selected_ids() -> list[int]:
    return load()["selected"]


def select(values) -> dict:
    """Tick these jobs. Returns every job ticked now, and any number that is no job in the list."""
    wanted = ids(values)
    known = _known(wanted)
    with _lock:
        data = load()
        have = set(data["selected"])
        data["selected"] += [i for i in wanted if i in known and i not in have]
        _save(data)
    return {"ids": data["selected"], "unknown": [i for i in wanted if i not in known]}


def unselect(values) -> list[int]:
    """Untick these jobs. Returns every job still ticked."""
    gone = set(ids(values))
    with _lock:
        data = load()
        data["selected"] = [i for i in data["selected"] if i not in gone]
        _save(data)
    return data["selected"]


def clear_selection() -> list[int]:
    with _lock:
        data = load()
        data["selected"] = []
        _save(data)
    return []


# -- the jobs added to Auto-apply ------------------------------------------------------------

def migrate_queue() -> int:
    """Jobs added to Auto-apply while the queue was kept in this file go to the database, once."""
    from backend.apply import execution
    with _lock:
        data = load()
        waiting = [e["job_id"] for e in data["queue"]]
        if not waiting:
            return 0
        # An import cut short (added to the database, the file not yet emptied) is not done twice:
        # a job the database has ever had -- on its way, or finished since -- is not added again.
        known = execution.known_job_ids()
        new = [j for j in waiting if j not in known]
        if new:
            execution.add(new)
        data["queue"] = []
        _save(data)
    return len(new)


def transfer(cfg) -> dict:
    """Add the ticked jobs that can go to the Auto-apply list; leave ticked, with the reason, the
    ones that cannot. Sends nothing: the list is applied to only when the person presses Apply
    to these. A job on the list already is not added twice."""
    from backend.apply import execution
    migrate_queue()
    with _lock:
        data = load()
        result = check(cfg, data["selected"])
        movable = [j["id"] for j in result["jobs"] if j["state"] != BLOCKED]
        added, already = execution.add(movable)
        moved = set(movable)
        data["selected"] = [i for i in data["selected"] if i not in moved]
        _save(data)
    # Each one added starts with the decision the review showed, written down as its first step.
    execution.keep_decisions({j["id"]: j["decision"] for j in result["jobs"] if j["id"] in set(added)})
    kept = [j for j in result["jobs"] if j["state"] == BLOCKED]
    return {"added": len(added), "already": len(already), "kept": kept,
            "selected": data["selected"], "queued": len(execution.live_job_ids(execution.LIVE))}


# -- where each job stands -------------------------------------------------------------------

class _NoDetails:
    """Your details before they are filled in: no profile rule can rule a job out yet, and the
    project's daily limit is the only one. A run refuses to start until they are filled in."""
    authorisation: dict = {}
    max_per_day = 10 ** 6
    stop_before_submit = True


def _applicant():
    """The person's details and what is missing from them, if anything."""
    from backend.apply import runner
    from backend.apply.profile import ProfileIncomplete
    try:
        applicant = runner.load(PROJECT_ROOT / "backend" / "config" / "applicant.yaml")
    except ProfileIncomplete as exc:
        return _NoDetails(), str(exc)
    problems = runner.check_ready(applicant)
    return applicant, ("; ".join(problems) if problems else None)


def _clock(when: datetime, now: datetime) -> str:
    """"9:15 PM", or "tomorrow at 2:10 AM", in the person's own time."""
    local, today = when.astimezone(), now.astimezone().date()
    text = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    return text if local.date() == today else f"tomorrow at {text}"


def _later_reason(job, last_applied: dict, taken: set[str], left: int, now: datetime) -> str:
    """Why a job that can go is not on the next run: the company was applied to lately, another
    chosen job at it goes first, or today's limit."""
    from backend.apply.queue import DEFAULT_COOLDOWN_HOURS, _company_key
    from data_engineering.scraper.lifecycle import as_utc
    key, hours = _company_key(job.company), DEFAULT_COOLDOWN_HOURS
    last = max((as_utc(w) for name, w in last_applied.items()
                if w is not None and _company_key(name) == key), default=None)
    if last is not None and now - last < timedelta(hours=hours):
        return (f"you applied to {job.company} in the last {hours} hours: "
                f"it can go after {_clock(last + timedelta(hours=hours), now)}")
    if key in taken:
        return f"another of your chosen jobs at {job.company} goes first: one a company each run, {hours} hours apart"
    if left <= 0:
        return "today's limit is reached: it goes on a run tomorrow"
    return f"over today's limit ({left} left today): it goes on a later run"


def review(cfg) -> dict:
    """The ticked jobs as the next run will see them once added: beside the jobs already waiting on
    the Auto-apply list, which count for one a company and today's limit too; and one on the list
    already says so, with its state there -- adding it again adds nothing. (Until 25 Oct 2026 the
    review looked at the ticked jobs alone, and said "goes on the next run" of a job whose company
    had a better one waiting, and of one on the list already, even one that may have gone.)"""
    from backend.apply import execution
    from data_engineering.db.models import ApplyItem
    from data_engineering.db.session import get_session
    picked = selected_ids()
    with get_session() as session:
        listed = {j: s for j, s in session.query(ApplyItem.job_id, ApplyItem.state)
                  .filter(ApplyItem.state.in_(execution.LIVE))}
    waiting = [j for j in execution.live_job_ids((execution.QUEUED,)) if j not in set(picked)]
    result = check(cfg, picked, alongside=waiting)
    for row in result["jobs"]:
        if row["id"] in listed and row["state"] != BLOCKED:
            row["state"] = LISTED
            row["reason"] = f"on your Auto-apply list already ({execution.LABELS[listed[row['id']]].lower()})"
    result["jobs"].sort(key=lambda r: _RANK[r["state"]])          # stable: each keeps its place within
    result.update({s: sum(r["state"] == s for r in result["jobs"]) for s in STATES})
    return result


def check(cfg, job_ids: list[int], *, alongside=()) -> dict:
    """Where each of these jobs stands: ready (goes on the next run), later (can go, but not on
    the next run), needs (its posting asks what the person's profile leaves unanswered: it waits
    for the answer), answers (its form asks questions nothing answers yet: it waits for the person's
    answers on the Questions page), or blocked (cannot go, and why); and each one's eligibility decision
    (runner.decision: the posting's own words, what is unknown, what to know). The next run is
    worked out by the same code that makes it (runner.chosen_queue), so this never says one thing
    and the run does another; `alongside` are the jobs it takes beside these (the ones waiting on
    the list), not reported. Opens nothing and sends nothing.

    Ready jobs come first, in the order the run takes them; then later ones, then those waiting
    for an answer, then blocked ones, each in the order they were chosen."""
    from backend.apply import prepare, runner
    from backend.apply.queue import _company_key
    from backend.core.jobage import age_label
    from data_engineering.db.session import get_session

    applicant, missing = _applicant()
    entries = prepare.load()
    search = runner.searched_places(cfg)
    now = datetime.now(timezone.utc)
    rows = []
    with get_session() as session:
        left = runner.remaining_today(cfg, applicant,
                                      runner._applied_since(session, runner.start_of_today_utc()))
        found = runner.jobs_by_id(session, job_ids)
        going = runner.chosen_queue(cfg, applicant, session, [*alongside, *job_ids], max(left, 0), search=search)
        order = {c.job_id: n for n, c in enumerate(going)}
        taken = {_company_key(c.company) for c in going}
        last_applied = runner._last_applied_by_company(session)
        for job_id in job_ids:
            job = found.get(job_id)
            if job is None:
                rows.append({"id": job_id, "title": "", "company": "", "location": "", "board": "",
                             "score": None, "age": "", "status": "", "state": BLOCKED,
                             "reason": "this job is no longer in your jobs list", "decision": None})
                continue
            why = runner.blocked_reason(cfg, job, applicant, search, any_board=True)
            verdict = runner.decision(cfg, job, applicant, search)
            waiting = [] if why or not verdict.goes else runner.questions_waiting(job, applicant, entries)
            if why:
                state = BLOCKED
            elif not verdict.goes:
                state, why = NEEDS, "; ".join(verdict.unknowns)
            elif waiting:
                state, why = ANSWERS, (f"{len(waiting)} required question{'s' if len(waiting) != 1 else ''} on its "
                                       "form (Questions page)")
            elif job_id in order:
                state, why = READY, ""
            else:
                state, why = LATER, _later_reason(job, last_applied, taken, left, now)
            rows.append({"id": job.id, "title": job.title, "company": job.company,
                         "location": job.location or "", "board": job.source,
                         "score": None if job.ats_match_score is None else round(job.ats_match_score * 100),
                         "age": age_label(job), "status": job.status, "state": state, "reason": why,
                         "decision": verdict.as_dict(), "assisted": runner.assisted(job)})
    rows.sort(key=lambda r: (_RANK[r["state"]], order.get(r["id"], 0)))
    counts = {s: sum(r["state"] == s for r in rows) for s in STATES}
    return {"jobs": rows, **counts, "left_today": max(left, 0), "details_missing": missing,
            "auto_submit": not applicant.stop_before_submit and not execution_hold()}


def execution_hold() -> bool:
    from backend.apply.execution import auto_submit_held
    return auto_submit_held()
