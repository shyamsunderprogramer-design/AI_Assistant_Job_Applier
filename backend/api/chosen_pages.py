"""Choosing jobs to apply to: ticked on the Jobs page, reviewed, then added to Auto-apply.

None of these routes sends an application. The only one that starts a run that applies is
/apply/chosen/start, the Auto-apply page's "Apply to these" (backend/apply/chosen.py);
/apply/chosen/prepare, "Prepare these", makes the documents ready and opens no form.
"""

from __future__ import annotations

from flask import jsonify, request

from backend.api.app import TASKS, app, cfg, start_task, task_pid

BUSY = ("An apply run is going already, and its browser window is open. "
        "Finish it or close that window, then press this again.")


def _body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@app.get("/selection")
def selection():
    """The jobs ticked on the Jobs page, kept until unticked or cleared."""
    from backend.apply import chosen
    return jsonify(ok=True, ids=chosen.selected_ids())


@app.post("/selection")
def selection_change():
    """Tick jobs ({"add": [ids]}), untick them ({"remove": [ids]}) or untick all ({"clear": true})."""
    from backend.apply import chosen
    body = _body()
    actions = [k for k in ("add", "remove", "clear") if k in body]
    if len(actions) != 1:
        return jsonify(ok=False, error='Send one of "add", "remove" or "clear".'), 400
    cfg()                                        # the database the job numbers are checked in
    try:
        if "add" in body:
            return jsonify(ok=True, **chosen.select(body["add"]))
        if "remove" in body:
            return jsonify(ok=True, ids=chosen.unselect(body["remove"]), unknown=[])
        if body["clear"] is not True:
            return jsonify(ok=False, error='"clear" takes true.'), 400
        return jsonify(ok=True, ids=chosen.clear_selection(), unknown=[])
    except chosen.NotChosen as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.get("/selection/review")
def selection_review():
    """Each ticked job and whether it can go: on the next run, later, on the Auto-apply list
    already, or not at all and why."""
    from backend.apply import chosen, execution
    return jsonify(ok=True, **chosen.review(cfg()), queued=len(execution.live_job_ids(execution.LIVE)))


@app.post("/selection/transfer")
def selection_transfer():
    """Add the ticked jobs that can go to Auto-apply's list. Sends nothing."""
    from backend.apply import chosen
    return jsonify(ok=True, **chosen.transfer(cfg()))


@app.get("/apply/chosen")
def apply_chosen():
    """The Auto-apply page's "Your chosen jobs": each one's state and what it needs, the run, and
    the latest activity (backend/apply/execution.py). Brings the list up to date as it reads it."""
    from backend.apply import execution
    return jsonify(ok=True, **execution.view(cfg()), running=bool(task_pid("apply-chosen") or task_pid("prepare-chosen")),
                   preparing=task_pid("prepare-chosen") is not None)


def _whole(value, default=None):
    """A non-negative whole number from a request, or `default`."""
    return int(value) if isinstance(value, str) and value.isdigit() and len(value) < 16 else default


@app.get("/apply/chosen/stream")
def apply_chosen_stream():
    """Each event as it happens, for the Auto-apply page (server-sent events, Phase 2, 25 Oct 2026).
    The page says where it got to -- `after`, or the browser's own Last-Event-ID when it reconnects,
    which is newer -- and is sent the rest, none twice. The stream ends after a few minutes and the
    browser comes back by itself. `seconds` and `every` (bounded) are for tests."""
    from flask import Response, stream_with_context

    from backend.apply import execution
    after = _whole(request.headers.get("Last-Event-ID"), _whole(request.args.get("after"), 0))
    seconds = min(max(_whole(request.args.get("seconds"), 300), 1), 600)
    every = min(max(_whole(request.args.get("every_ms"), 500), 50), 5000) / 1000
    cfg()                                        # the database is set up before the first read
    return Response(stream_with_context(execution.stream(after, seconds=seconds, every=every)),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/apply/chosen/events")
def apply_chosen_events():
    """The events after `after`, oldest first -- a page catching up without the stream -- or, given
    `item`, that entry's whole history, for its timeline."""
    from backend.apply import execution
    cfg()
    item = _whole(request.args.get("item"))
    if request.args.get("item") is not None and item is None:
        return jsonify(ok=False, error='"item" is an entry\'s number.'), 400
    events = execution.events_after(_whole(request.args.get("after"), 0), item_id=item, limit=500 if item else 200)
    return jsonify(ok=True, events=events, last_event_id=execution.last_event_id())


def _paid_refusal(state: dict, what: str):
    """A writer that bills per use (set in Settings) writes only once the person says yes to it,
    ({"paid": true}) -- never silently (26 Oct 2026). The refusal names it, for the page to ask."""
    if state["writer"]["free"] or _body().get("paid") is True:
        return None
    label = state["writer"]["label"]
    return jsonify(ok=False, paid_writer=label,
                   error=f"{what} would use {label}, which bills per use (set in Settings)."), 409


def _paid_args(task: str) -> list[str]:
    return TASKS[task] + (["--paid-ok"] if _body().get("paid") is True else [])


@app.post("/apply/chosen/start")
def apply_chosen_start():
    """Apply to these, or Resume: the jobs the person chose, and no others. Refused while another
    apply run's browser is open or a run is making documents ready, before the person's details
    are filled in, and when none can go."""
    from backend.api.auto_apply import APPLY_TASKS
    from backend.apply import execution
    if next((t for t in (*APPLY_TASKS, "prepare-chosen") if task_pid(t)), None) or execution.run_alive():
        return jsonify(ok=False, error=BUSY), 409
    state = execution.view(cfg())
    if state["details_missing"]:
        return jsonify(ok=False, error="Fill in your details first (Settings → Your details): "
                                       f"{state['details_missing']}"), 409
    if not state["next_run"]:
        return jsonify(ok=False, error="None of your chosen jobs can go right now: "
                                       "the list says why for each."), 409
    refused = _paid_refusal(state, "A resume or cover letter made on the way")
    if refused:
        return refused
    return start_task("apply-chosen", _paid_args("apply-chosen"))


@app.post("/apply/chosen/prepare")
def apply_chosen_prepare():
    """Prepare these: each chosen job that can go has its resume and cover letter made ready, and
    written down -- each file and its checksum -- so its form is given exactly those. Opens no form
    and sends nothing. Refused while a run is going, before the person's details are filled in, and
    when every one that can go is ready already."""
    from backend.api.auto_apply import APPLY_TASKS
    from backend.apply import execution
    if next((t for t in (*APPLY_TASKS, "prepare-chosen") if task_pid(t)), None) or execution.run_alive():
        return jsonify(ok=False, error="A run is going already (applying, or making documents ready). "
                                       "Let it finish or pause it, then press this again."), 409
    state = execution.view(cfg())
    if state["details_missing"]:
        return jsonify(ok=False, error="Fill in your details first (Settings → Your details): "
                                       f"{state['details_missing']}"), 409
    if not state["to_prepare"]:
        return jsonify(ok=False, error="Nothing to make ready: each chosen job that can go has its documents "
                                       "already, or none can go (the list says why for each)."), 409
    refused = _paid_refusal(state, "Tailoring and cover letters")
    if refused:
        return refused
    return start_task("prepare-chosen", _paid_args("prepare-chosen"))


@app.post("/apply/chosen/pause")
def apply_chosen_pause():
    """Stop before the next form opens; the form on screen is finished or closed first."""
    from backend.apply import execution
    if not execution.request_pause():
        return jsonify(ok=False, error="Nothing is being applied to right now."), 409
    return jsonify(ok=True)


def _item(body: dict) -> int | None:
    value = body.get("item")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _change(fn, *args):
    from backend.apply import execution
    try:
        fn(*args)
    except execution.QueueError as exc:
        return jsonify(ok=False, error=str(exc)), 409
    return jsonify(ok=True)


@app.post("/apply/chosen/cancel")
def apply_chosen_cancel():
    """Take one job off before it goes ({"item": id}), or every one still waiting ({"all": true})."""
    from backend.apply import execution
    body = _body()
    if body.get("all") is True:
        return jsonify(ok=True, cancelled=execution.cancel_waiting())
    item = _item(body)
    return _change(execution.cancel, item) if item else (jsonify(ok=False, error='Send "item" or "all".'), 400)


@app.post("/apply/chosen/retry")
def apply_chosen_retry():
    """Put a job that failed, was skipped or cancelled back in the queue ({"item": id})."""
    from backend.apply import execution
    item = _item(_body())
    return _change(execution.retry, item) if item else (jsonify(ok=False, error='Send "item".'), 400)


@app.post("/apply/chosen/confirm")
def apply_chosen_confirm():
    """The person's word on a job that may have gone ({"item": id, "went": true or false})."""
    from backend.apply import execution
    body = _body()
    item, went = _item(body), body.get("went")
    if item is None or not isinstance(went, bool):
        return jsonify(ok=False, error='Send "item" and "went": true or false.'), 400
    return _change(execution.confirm, item, went)


@app.post("/apply/chosen/clear")
def apply_chosen_clear():
    """Finished jobs off the page; the database keeps what happened to them."""
    from backend.apply import execution
    return jsonify(ok=True, hidden=execution.hide_finished())
