"""Every tailored resume made, to find, reopen, compare and clear out.

Each tailoring is a run folder (data/tailoring/runs/<id>/) holding its
documents, its result and its inputs; nothing else lists them. This reads
them all: for each, when, for which job and company, the ATS score before
and after, the pages, the template, whether its checks passed, and whether
it is the resume a job's application attaches (that one cannot be deleted
here -- the job would lose its resume).

Compare puts two versions side by side, line by line: what one has that the
other does not, from the same plan both writers draw.
"""

from __future__ import annotations

import difflib
import json
import shutil
import sqlite3
from pathlib import Path

from backend.config.loader import PROJECT_ROOT
from ml.tailoring.pipeline import JOBS, RUNS, folder_of, job_of_run


def _in_use() -> dict[str, int]:
    """run id -> the job whose application attaches it."""
    out = {}
    if JOBS.exists():
        for f in JOBS.glob("*.json"):
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            run = rec.get("best") or rec.get("run")
            if run and f.stem.isdigit():
                out[run] = int(f.stem)
    return out


def _jobs(ids: set[int]) -> dict[int, dict]:
    if not ids:
        return {}
    try:
        db = sqlite3.connect(f"file:{PROJECT_ROOT / 'data' / 'jobs.db'}?mode=ro", uri=True)
        marks = ",".join("?" * len(ids))
        rows = db.execute(f"select id, title, company from jobs where id in ({marks})", tuple(ids)).fetchall()
        db.close()
    except sqlite3.Error:
        return {}
    return {r[0]: {"title": r[1], "company": r[2]} for r in rows}


def runs() -> list[dict]:
    if not RUNS.exists():
        return []
    used = _in_use()
    items, job_ids = [], set()
    for folder in sorted(RUNS.iterdir(), reverse=True):
        if not folder.is_dir():
            continue
        try:
            result = json.loads((folder / "result.json").read_text())
        except (OSError, ValueError):
            result = None
        try:
            progress = json.loads((folder / "progress.json").read_text())
        except (OSError, ValueError):
            progress = {}
        job = job_of_run(folder) or {}
        company_file = folder / "inputs" / "company.txt"
        ba = (result or {}).get("before_after") or {}
        draft = (result or {}).get("draft") or {}
        item = {
            "id": folder.name,
            "when": f"{folder.name[:4]}-{folder.name[4:6]}-{folder.name[6:8]} {folder.name[9:11]}:{folder.name[11:13]}",
            "title": (result or {}).get("title") or "",
            "company": company_file.read_text().strip() if company_file.exists() else "",
            "job_id": job.get("job_id"),
            "attached_to": used.get(folder.name),
            "status": "done" if result else ("failed" if progress.get("error") else
                                             "running" if progress and not progress.get("done") else "incomplete"),
            "error": (progress.get("error") or {}).get("message", "") if not result else "",
            "ats": ba.get("ats") or [],
            "pages": len(((result or {}).get("pages") or {}).get("pdf") or []),
            "template": (draft.get("template") if isinstance(draft, dict) else "") or "classic",
            "length": ((result or {}).get("length") or {}).get("choice", ""),
            "checks_ok": (result or {}).get("checks_ok"),
            "rewritten": ba.get("rewritten"),
            "edited": bool((draft.get("overrides") if isinstance(draft, dict) else None)),
            "has_docx": (folder / "tailored-resume.docx").exists(),
        }
        if item["job_id"]:
            job_ids.add(int(item["job_id"]))
        items.append(item)
    known = _jobs(job_ids)
    for item in items:
        j = known.get(int(item["job_id"])) if item["job_id"] else None
        if j:
            item["company"] = item["company"] or j["company"] or ""
            item["title"] = item["title"] or j["title"] or ""
    return items


def lines_of(run_id: str) -> tuple[list[str], bool]:
    """The run's resume as the lines both writers draw."""
    from ml.tailoring.pipeline import editable_lines
    folder = folder_of(run_id)
    if folder is None or not (folder / "result.json").exists():
        raise ValueError(f"No finished resume called {run_id}.")
    try:
        return [" ".join(l["text"].split()) for l in editable_lines(folder)
                if l["kind"] not in ("name", "contact")], False
    except (StopIteration, OSError, ValueError, KeyError):
        # A run whose inputs were not kept: its PDF's text, line by line.
        from ml.tailoring.reader import read_document
        pdf = folder / "tailored-resume.pdf"
        if not pdf.exists():
            raise ValueError(f"{run_id} has no resume to compare.")
        lines = [b.text.strip() for b in read_document(pdf).blocks if b.text.strip()][3:]
        return [l.lstrip("• ").strip() for l in _joined(lines)], True


SIMILAR = 0.45          # a reworded line, not a different one


def _pair(left: list[str], right: list[str]) -> list[dict]:
    """Within a block that changed, each line beside its reworded twin, in order.

    Paired by position, a cut line pushed every line after it beside the
    wrong neighbour. Each left line takes the most similar right line still
    ahead of it; anything unpaired stands alone on its side.
    """
    rows, j = [], 0
    for x in left:
        best, score = None, SIMILAR
        for k in range(j, len(right)):
            r = difflib.SequenceMatcher(None, x.lower(), right[k].lower()).ratio()
            if r >= score:
                best, score = k, r
        if best is None:
            rows.append({"op": "only_a", "a": x, "b": ""})
            continue
        rows += [{"op": "only_b", "a": "", "b": y} for y in right[j:best]]
        rows.append({"op": "changed", "a": x, "b": right[best]})
        j = best + 1
    rows += [{"op": "only_b", "a": "", "b": y} for y in right[j:]]
    return rows


def _word_diff(a: str, b: str) -> tuple[list[list], list[list]]:
    """[[text, changed], ...] for each side: the words one has and the other does not."""
    wa, wb = a.split(" "), b.split(" ")
    pa, pb = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if i2 > i1:
            pa.append([" ".join(wa[i1:i2]), op != "equal"])
        if j2 > j1:
            pb.append([" ".join(wb[j1:j2]), op != "equal"])
    return pa, pb


def _joined(lines: list[str]) -> list[str]:
    """A PDF's wrapped lines put back into the lines that were written.

    A line joins the one before it unless it opens something: a bullet, a
    heading in capitals, a job or degree line with dates, or a "Label:" row
    -- or the line before it ended a sentence.
    """
    from ml.tailoring.structure import DATE_RANGE
    out: list[str] = []
    for line in lines:
        if out and DATE_RANGE.fullmatch(line.strip()):
            out[-1] = f"{out[-1]} {line}"            # a job's dates, drawn at the right margin
            continue
        opens = (line.startswith(("•", "-", "▪")) or (line.isupper() and len(line) < 70)
                 or DATE_RANGE.search(line) or (": " in line[:45] and line[:1].isupper()))
        prev = out[-1] if out else ""
        closed = (prev.endswith((".", "!", "?", ":")) or DATE_RANGE.search(prev)
                  or (prev.isupper() and len(prev) < 70))       # a heading or a job line takes no followers
        if out and not opens and not closed:
            out[-1] = f"{prev} {line}"
        else:
            out.append(line)
    return out


def compare(a: str, b: str) -> dict:
    """Line by line: {rows: [{op, a, b}], same, only_a, only_b}. op: same | changed | only_a | only_b."""
    (la, rough_a), (lb, rough_b) = lines_of(a), lines_of(b)
    rows = []
    # Matched ignoring case: the PDF prints a heading in capitals that the plan writes in title case.
    matcher = difflib.SequenceMatcher(None, [x.lower() for x in la], [y.lower() for y in lb], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            rows += [{"op": "same", "a": la[i], "b": lb[j1 + k]} for k, i in enumerate(range(i1, i2))]
        elif op == "replace":
            rows += _pair(la[i1:i2], lb[j1:j2])
        elif op == "delete":
            rows += [{"op": "only_a", "a": la[i], "b": ""} for i in range(i1, i2)]
        else:
            rows += [{"op": "only_b", "a": "", "b": lb[j]} for j in range(j1, j2)]
    for r in rows:
        if r["op"] == "changed":
            r["a_parts"], r["b_parts"] = _word_diff(r["a"], r["b"])
    count = lambda op: sum(r["op"] == op for r in rows)   # noqa: E731
    return {"rows": rows, "same": count("same"), "changed": count("changed"),
            "only_a": count("only_a"), "only_b": count("only_b"),
            # A run whose inputs were not kept is compared by its PDF's wrapped lines: rougher.
            "rough": rough_a or rough_b}


def delete(run_id: str) -> None:
    folder = folder_of(run_id)
    if folder is None:
        raise ValueError("No such tailored resume.")
    job = _in_use().get(run_id)
    if job:
        raise PermissionError(f"This is the resume job {job}'s application attaches. "
                              "Make another one for that job first, or keep it.")
    shutil.rmtree(folder)
