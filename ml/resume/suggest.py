"""What a posting asks for that the resume does not show, with a line drafted for each.

The job page lists them before tailoring: each one ticked, with the job it
would go under and a line the person can edit. They untick anything that is
not true for them and press "Add all"; what they add becomes part of their
resume (ml/resume/additions.py) for this job and every later one, so the same
skill is asked about once, not once per posting.

Nothing is added without that press. Suggestions are kept per job in
data/suggestions/<job id>.json and made again only when the resume or the
posting changes; the daily run prepares them for the top new jobs.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

FOLDER = PROJECT_ROOT / "data" / "suggestions"
MAX_ITEMS = 12

SYSTEM = """You draft resume bullet points. For each skill below, write ONE bullet the
person could add under one of their jobs, so the resume shows that skill.

Rules:
- Pick the job where the skill fits the work best (by its index).
- Match that job's existing bullets in subject and tone; it must read as the same person's work.
- Plain, natural English, like a person wrote it: start with a past-tense verb, 12-24 words,
  one sentence. No buzzwords (spearheaded, leveraged, utilized, robust, seamless, cutting-edge).
- Name the skill exactly as given, once.
- No percentages, money, headcounts or other numbers.
- No employer or product names other than the skill itself.

Reply with JSON only: {"items": [{"skill": "...", "job": <index>, "line": "..."}]}"""


def _path(job_id: int) -> Path:
    return FOLDER / f"{job_id}.json"


def _key(resume_text: str, jd_text: str) -> str:
    return hashlib.sha256((resume_text + "\x1f" + jd_text).encode()).hexdigest()[:16]


def role_blocks(resume) -> list[tuple[str, list[str]]]:
    """Each job line with a few of its bullets, most recent first."""
    from ml.resume.additions import roles
    from ml.resume.parser import strip_bullet
    from ml.resume.writer import _is_role_line
    wanted = roles(resume)
    blocks: dict[str, list[str]] = {r: [] for r in wanted}
    for section in resume.sections:
        current = None
        for line in section.lines:
            text = line.strip()
            if text in blocks:
                current = text
            elif current and text and not _is_role_line(text):
                blocks[current].append(strip_bullet(text))
            elif _is_role_line(text):
                current = None
    return [(r, blocks[r][:4]) for r in wanted]


def missing(resume, jd_text: str) -> list[dict]:
    """Skills the posting asks for that the resume does not show in its work.

    By rules, no model: what the matching calls Not found, and what it calls
    Unclear because it sits only in a skills list. Required ones first.
    """
    from ml.tailoring import matching, requirements
    from ml.tailoring.reader import read_document
    from ml.tailoring.structure import build

    reqs, _ = requirements.extract(jd_text, None, use_model=False)
    with tempfile.TemporaryDirectory() as tmp:
        text_file = Path(tmp) / "resume.txt"
        text_file.write_text(resume.text(), encoding="utf-8")
        model = build(read_document(text_file))
    out, seen = [], set()
    for req in reqs:
        if req.category not in ("tool", "skill", "certification"):
            continue
        status = (matching.rule_match(req, model) or ("not_found", [], ""))[0]
        if status not in ("not_found", "unclear"):
            continue
        name = req.original.strip()
        if name.lower() in seen or len(name.split()) > 4:
            continue                       # a sentence is a duty, not a skill to add
        seen.add(name.lower())
        out.append({"skill": name, "required": req.priority == "required",
                    "why": "only in your skills list" if status == "unclear" else "not on your resume"})
    out.sort(key=lambda m: not m["required"])
    return out[:MAX_ITEMS]


def draft(items: list[dict], blocks: list[tuple[str, list[str]]], cfg=None) -> list[dict]:
    """A line under a job for each item. Without a model, a plain starting line to edit."""
    if not items or not blocks:
        return []
    jobs = "\n".join(f"[{i}] {role}\n" + "\n".join(f"    - {b}" for b in bullets)
                     for i, (role, bullets) in enumerate(blocks))
    user = f"The person's jobs:\n{jobs}\n\nSkills to show:\n" + "\n".join(f"- {m['skill']}" for m in items)
    drafted: dict[str, dict] = {}
    try:
        from ml.resume.llm import complete
        reply = complete(SYSTEM, user, cfg, max_tokens=3000).text
        body = json.loads(reply[reply.index("{"): reply.rindex("}") + 1])
        for d in body.get("items", []):
            if isinstance(d, dict) and d.get("skill") and d.get("line"):
                drafted[str(d["skill"]).lower()] = d
    except Exception:
        drafted = {}
    out = []
    for m in items:
        d = drafted.get(m["skill"].lower(), {})
        index = d.get("job") if isinstance(d.get("job"), int) and 0 <= d.get("job") < len(blocks) else 0
        line = re.sub(r"\s+", " ", str(d.get("line") or "")).strip()
        if m["skill"].lower() not in line.lower():
            line = f"Used {m['skill']} in day-to-day work on this team."     # a start; edit it
            drafted_by_model = False
        else:
            drafted_by_model = True
        out.append({**m, "role": blocks[index][0], "line": line, "drafted": drafted_by_model})
    return out


def for_job(cfg, job, refresh: bool = False) -> dict:
    """The suggestions for one job, made or loaded."""
    from ml.resume.pipeline import load_base_resume
    resume = load_base_resume(cfg)                       # with what was added before
    jd_text = f"{job.title}\n\n{job.description or ''}"
    key = _key(resume.text(), jd_text)
    path = _path(job.id)
    if not refresh:
        try:
            saved = json.loads(path.read_text())
            if saved.get("key") == key:
                return saved
        except (OSError, ValueError):
            pass
    blocks = role_blocks(resume)
    items = draft(missing(resume, jd_text), blocks, cfg)
    result = {"key": key, "items": items, "roles": [r for r, _ in blocks]}
    FOLDER.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1))
    return result


def accept(cfg, job_id: int, chosen: list[dict]) -> tuple[int, list[str]]:
    """Add the lines the person kept. Returns how many, and why any were not."""
    from ml.resume.additions import NotAdded, add
    from ml.resume.pipeline import load_base_resume
    resume = load_base_resume(cfg)
    added, problems = 0, []
    for item in chosen:
        try:
            add(resume, str(item.get("role") or ""), str(item.get("line") or ""),
                str(item.get("skill") or ""), confirmed=True)
            added += 1
        except NotAdded as exc:
            problems.append(f"{item.get('skill')}: {exc}")
    _path(job_id).unlink(missing_ok=True)                # the resume changed; ask again fresh
    return added, problems
