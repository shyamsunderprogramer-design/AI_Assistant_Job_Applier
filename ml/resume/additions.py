"""Experience the person adds from a job page: "I've used this".

When a posting asks for a tool the master resume does not show, the person
may simply never have written it down. They say where they used it and what
they did, in their own words, and tick that it is true; the line is kept here
and merged under that job every time the master resume is read. Tailoring,
the fabrication guard and the ATS check then all treat it as part of the
resume -- which it now is, because the person wrote it.

The person's own resume file is never edited. Additions live beside it in
ml/resume/additions.yaml (gitignored), one entry each:

    - role: "SENIOR DEVOPS ENGINEER, ACME, USA     Jan 2022 – Present"
      line: "Defined Protobuf schemas for service-to-service contracts."
      tool: "protobuf"
      added: "2026-09-30"
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

ADDITIONS_PATH = PROJECT_ROOT / "ml" / "resume" / "additions.yaml"
MAX_LINE = 300


class NotAdded(ValueError):
    """The addition was refused, with a reason the person can act on."""


def load(path: Path | None = None) -> list[dict]:
    import yaml
    target = Path(path or ADDITIONS_PATH)
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or []
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("role") and d.get("line")]


def _save(entries: list[dict], path: Path | None = None) -> None:
    import yaml
    target = Path(path or ADDITIONS_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# Lines you added from job pages (\"I've used this\"). Each is merged under\n"
        "# its job whenever your resume is read. Edit or delete freely.\n"
        + yaml.safe_dump(entries, sort_keys=False, allow_unicode=True), encoding="utf-8")


def roles(resume) -> list[str]:
    """The job lines of the resume, most recent first, as written."""
    from ml.resume.writer import _is_role_line
    found = []
    for section in resume.sections:
        low = section.heading.lower()
        if section.heading == "HEADER" or any(w in low for w in ("education", "certif", "license",
                                                                  "award", "course", "training")):
            continue                        # a name, a degree or a certificate is not a job
        for line in section.lines:
            text = line.strip()
            # A job line carries its dates; "JORDAN EXAMPLE" and group labels do not.
            if _is_role_line(text) and re.search(r"(19|20)\d\d", text) and text not in found:
                found.append(text)
    return found


def add(resume, role: str, line: str, tool: str, confirmed: bool,
        path: Path | None = None) -> dict:
    """Keep one line of the person's own experience. Raises NotAdded if it cannot be."""
    line = re.sub(r"\s+", " ", (line or "")).strip().lstrip("-•* ").strip()
    tool = (tool or "").strip()
    if not confirmed:
        raise NotAdded("Tick that you have used this in real work — the resume is a statement of fact.")
    if role not in roles(resume):
        raise NotAdded("Pick one of the jobs on your resume.")
    if len(line.split()) < 5:
        raise NotAdded("Write a full line about what you did — at least a few words.")
    if len(line) > MAX_LINE:
        raise NotAdded(f"Keep it to one line (under {MAX_LINE} characters).")
    if tool and tool.lower() not in line.lower():
        raise NotAdded(f"Mention {tool} in the line, so it is clear where you used it.")
    if not line.endswith((".", "!", "?")):
        line += "."
    entries = load(path)
    if any(e["role"] == role and e["line"].lower() == line.lower() for e in entries):
        raise NotAdded("That line is already on your resume.")
    entry = {"role": role, "line": line, "tool": tool, "added": date.today().isoformat()}
    _save(entries + [entry], path)
    return entry


def remove(role: str, line: str, path: Path | None = None) -> bool:
    entries = load(path)
    kept = [e for e in entries if not (e["role"] == role and e["line"] == line)]
    if len(kept) == len(entries):
        return False
    _save(kept, path)
    return True


def apply(resume, path: Path | None = None):
    """Merge the additions into a parsed resume, each under its job. Returns the resume."""
    from ml.resume.writer import _is_role_line
    entries = load(path)
    if not entries:
        return resume
    for section in resume.sections:
        lines = section.lines
        for entry in entries:
            try:
                start = [l.strip() for l in lines].index(entry["role"])
            except ValueError:
                continue
            end = start + 1
            while end < len(lines) and not _is_role_line(lines[end].strip()):
                end += 1
            bullet = f"- {entry['line']}"
            if bullet not in lines[start:end]:
                lines.insert(end, bullet)
    # The text the guard and the ATS check read comes from the sections too.
    if hasattr(resume, "raw_text"):
        extra = "\n".join(e["line"] for e in entries)
        if extra not in resume.raw_text:
            resume.raw_text = resume.raw_text + "\n" + extra
    return resume
