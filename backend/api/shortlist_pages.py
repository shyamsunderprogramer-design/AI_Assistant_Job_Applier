"""The morning's picks on the Auto-apply page, and their settings on the Settings page (4 Nov 2026,
step 2 of the Morning shortlist plan; backend/apply/shortlist.py).

None of these routes prepares a document, opens a form or sends anything. Remove and Not today act on
the morning's picks only -- a job the person chose themselves is refused -- and cancel a pick's entry on
the chosen list only while it waits; Restore puts nothing on the list. Saving the settings writes
data/apply/shortlist_settings.json, never config.yaml.
"""

from __future__ import annotations

from flask import jsonify, request

from backend.api.app import app, cfg, start_task, task_pid


def _job_id():
    body = request.get_json(silent=True)
    value = body.get("job_id") if isinstance(body, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


@app.get("/apply/picks")
def apply_picks():
    """Today's picks, the jobs set aside, and why the others were left out. Reads only."""
    from backend.apply import execution, shortlist
    return jsonify(ok=True, **shortlist.view(cfg()), preparing=task_pid("prepare-chosen") is not None,
                   running=bool(task_pid("prepare-chosen") or task_pid("apply-chosen")) or execution.run_alive())


def _word(fn, *args):
    from backend.apply import execution, shortlist
    job_id = _job_id()
    if job_id is None:
        return jsonify(ok=False, error='Send "job_id".'), 400
    try:
        return jsonify(ok=True, **fn(job_id, *args))
    except (shortlist.PickError, execution.QueueError) as exc:
        return jsonify(ok=False, error=str(exc)), 409


@app.post("/apply/picks/remove")
def apply_picks_remove():
    """Never pick this job again; its entry on the list is cancelled while it waits ({"job_id": id})."""
    from backend.apply import shortlist
    return _word(shortlist.take_off, shortlist.REMOVED)


@app.post("/apply/picks/defer")
def apply_picks_defer():
    """Not today: left out until tomorrow, then considered again ({"job_id": id})."""
    from backend.apply import shortlist
    return _word(shortlist.take_off, shortlist.DEFERRED)


@app.post("/apply/picks/restore")
def apply_picks_restore():
    """Undo Remove or Not today: considered again from the next picking ({"job_id": id})."""
    from backend.apply import shortlist
    return _word(shortlist.bring_back)


def _settings_reply():
    from backend.apply import shortlist
    from backend.core.jobfields import JOB_TYPES
    s = shortlist.settings(cfg())
    return {"settings": {k: s[k] for k in shortlist.DEFAULTS if k != "max_considered"},
            "workplaces": list(shortlist.WORKPLACES), "job_types": JOB_TYPES}


@app.get("/apply/picks/settings")
def apply_picks_settings():
    return jsonify(ok=True, **_settings_reply())


@app.post("/apply/picks/settings")
def apply_picks_settings_save():
    """Keep the settings sent (only the keys sent change); nothing is kept if one is out of range."""
    from backend.apply import shortlist
    try:
        shortlist.save_settings(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, **_settings_reply())


@app.post("/apply/picks/prepare")
def apply_picks_prepare():
    """Prepare, pressed on the picks: the resume and cover letter made ready for that pick ({"job_id": id}),
    or for each of today's picks still waiting with none ({}), and written down, each file with its
    checksum -- by the same run as Prepare these, for those entries only. Opens no form, sends nothing.
    Refused while a run is going, before the person's details are filled in, when there is nothing to
    make, and -- until the person says yes ({"paid": true}) -- when the writer bills per use."""
    from backend.api.auto_apply import APPLY_TASKS
    from backend.api.chosen_pages import _paid_args, _paid_refusal
    from backend.apply import execution, shortlist
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    job_id = body.get("job_id")
    if job_id is not None and (not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0):
        return jsonify(ok=False, error='Send "job_id", or nothing for all of today\'s picks.'), 400
    if next((t for t in (*APPLY_TASKS, "prepare-chosen") if task_pid(t)), None) or execution.run_alive():
        return jsonify(ok=False, error="A run is going already (applying, or making documents ready). "
                                       "Let it finish or pause it, then press this again."), 409
    state = execution.view(cfg())
    if state["details_missing"]:
        return jsonify(ok=False, error="Fill in your details first (Settings → Your details): "
                                       f"{state['details_missing']}"), 409
    items = shortlist.to_prepare(job_id)
    if not items:
        return jsonify(ok=False, error="Nothing to make ready: " + (
            "that pick has its documents already, or is not waiting on your list." if job_id
            else "each of today's picks has its documents already, or none is waiting on your list.")), 409
    refused = _paid_refusal(state, "Tailoring and cover letters")
    if refused:
        return refused
    return start_task("prepare-chosen", _paid_args("prepare-chosen") + ["--items", ",".join(map(str, items))])
