"""The Applications pages: every application, and one page each with its stages."""

from __future__ import annotations

from flask import abort, jsonify, render_template, request, send_file

from backend.api.app import app


@app.get("/applications")
def applications_page():
    from backend.apply import tracker
    items = tracker.applications()
    counts = {s: sum(1 for a in items if a["status"] == s) for s in tracker.APPLIED_STATUSES}
    return render_template("applications.html", items=items, counts=counts)


@app.get("/applications/<int:job_id>")
def application_page(job_id: int):
    from backend.apply import tracker
    item = tracker.application(job_id)
    if item is None:
        abort(404)
    return render_template("application.html", a=item, stages=tracker.STAGES,
                           follow_up_days=tracker.FOLLOW_UP_DAYS)


@app.post("/applications/<int:job_id>/stage")
def application_add_stage(job_id: int):
    from backend.apply import tracker
    body = request.get_json(silent=True) or {}
    try:
        item = tracker.add_stage(job_id, str(body.get("stage") or ""), body.get("on") or None,
                                 str(body.get("note") or ""))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, application=item)


@app.post("/applications/<int:job_id>/stage/remove")
def application_remove_stage(job_id: int):
    from backend.apply import tracker
    try:
        item = tracker.remove_stage(job_id, int((request.get_json(silent=True) or {}).get("index", -1)))
    except (ValueError, TypeError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, application=item)


@app.get("/applications/<int:job_id>/file/<name>")
def application_file(job_id: int, name: str):
    from backend.apply import tracker
    path = tracker.evidence_file(job_id, name)
    if path is None:
        abort(404)
    kinds = {".pdf": "application/pdf", ".png": "image/png", ".txt": "text/plain",
             ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    return send_file(path, mimetype=kinds.get(path.suffix, "application/octet-stream"),
                     as_attachment=path.suffix == ".docx")
