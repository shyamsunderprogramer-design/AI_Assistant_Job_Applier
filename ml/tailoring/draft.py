"""Write the tailored draft from the uploaded resume and the match report.

The rewrite itself is the existing engine (ml/resume/tailor.py): it may only
rephrase, reorder and drop; it keeps every number, tense and title; the
fabrication guard rejects any skill, employer, date or figure the resume does
not contain; and a rewrite that copies the posting is put back. This module
adds what Resume Tailoring needs on top:

  * the match report's evidence, so the rewrite emphasises what the posting
    needs and the resume shows -- and is told plainly what the resume does
    NOT show, so it leaves those out;
  * its writing rules: grammar, spelling and flow; bullets as action + work
    + tool + result where the resume supports each part; details from other
    sections that strengthen a bullet; repetition removed;
  * the uploaded resume's own contact lines, never the app owner's profile;
  * a record of every change, for the Changes Made tab.

The uploaded file is never modified: the draft is new files beside it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

GUIDANCE = """# Resume Tailoring: how to write this draft
The match report below lists what the posting needs and the resume lines that show it.
- Put the most relevant supported skills and experience where a reader finds them first.
- Correct grammar, spelling, punctuation and awkward flow, keeping the meaning.
- Where the resume supports each part, write bullets as: action + work performed + skill or tool + result.
- You may strengthen a bullet with a detail that appears elsewhere in this resume (another section or
  bullet) when it is about the same work. Never with a detail that is not in the resume.
- Use the posting's wording only where it accurately describes what the resume says.
- Remove or combine repeated wording only when no useful evidence is lost.
- Keep every job title exactly as written. Add numbers or results only if the resume has them.

What the resume SHOWS (emphasise these):
{shown}

What the resume does NOT show (do not add or imply these):
{gaps}"""


@dataclass
class Draft:
    base: object                  # ml.resume.parser.Resume
    result: object                # ml.resume.tailor.TailorResult
    header: list[str]
    title: str
    changes: list[dict] = field(default_factory=list)
    accepted: bool = True
    problem: str = ""
    # What the model itself left out, before any line was cut for space: a refit starts here.
    model_omitted: list = field(default_factory=list)


def guess_title(jd_text: str) -> str:
    """The posting's first short line, as its job title; "this role" when there is none."""
    for line in (jd_text or "").splitlines():
        line = line.strip(" #*-•\t")
        if 2 <= len(line.split()) <= 10 and not line.endswith(".") and not re.search(r"\d{4}", line):
            return line
    return "this role"


def header_of(base) -> list[str]:
    section = next((s for s in base.sections if s.heading == "HEADER"), None)
    return [l for l in (section.lines[:4] if section else []) if l.strip()]


def guidance(report) -> str:
    shown = [f"- {m.requirement['original']}: “{m.evidence[0]['text'][:120]}”"
             for m in report.matches if m.status in ("direct", "related") and m.evidence]
    gaps = [f"- {m.requirement['original']}" for m in report.matches if m.status in ("unclear", "not_found")]
    return GUIDANCE.format(shown="\n".join(shown[:40]) or "- (nothing matched)",
                           gaps="\n".join(gaps[:40]) or "- (none)")


def evidence_lines(report) -> set[str]:
    """The resume lines that show a requirement the posting has (direct or related)."""
    return {e["text"] for m in report.matches if m.status in ("direct", "related")
            for e in (m.evidence or []) if e.get("text")}


def required_proof(report) -> list[set[str]]:
    """For each required item the resume shows directly, the lines that show it."""
    return [{e["text"] for e in m.evidence if e.get("text")}
            for m in report.matches
            if m.status == "direct" and (m.requirement or {}).get("priority") == "required" and m.evidence]


def _same_line(text: str) -> str:
    return " ".join((text or "").lower().lstrip("-•*·▪◦–— ").split())[:80]


def keep_required_wording(result, report) -> list[str]:
    """Undo a rewrite that drops the only words proving a required item.

    The rewritten bullet said the same work in other words, and "incident
    response" -- required, shown directly on the resume -- was in none of them.
    The bullet goes back to the person's own wording; returns the lines undone.
    """
    from ml.tailoring.synonyms import mentions
    rewrites = {_same_line(b.get("original")): b for b in result.bullets or [] if b.get("original")}
    undone = []
    for m in report.matches:
        req = m.requirement or {}
        if m.status != "direct" or req.get("priority") != "required":
            continue
        names = req.get("any_of") or [req.get("normalized") or ""]

        def says(text):
            return any(n and mentions(text or "", n) for n in names)
        proof = [e["text"] for e in m.evidence or [] if e.get("text") and says(e["text"])]
        if not proof or any(_same_line(p) not in rewrites or says(rewrites[_same_line(p)].get("tailored"))
                            for p in proof):
            continue
        bullet = rewrites.pop(_same_line(proof[0]))
        result.bullets.remove(bullet)
        undone.append(proof[0])
    return undone


def create(resume_path, jd_text: str, report, cfg=None, max_pages: int = 2, use_model: bool = True,
           extra: str = "", focus: list[str] | None = None) -> Draft:
    from ml.resume.fit import fit_to_pages
    from ml.resume.parser import parse_resume
    from ml.resume.tailor import TailorResult, tailor_resume

    base = parse_resume(resume_path)
    header = header_of(base)
    title = guess_title(jd_text)
    problem = ""
    try:
        if not use_model:
            raise RuntimeError("the writing model is switched off")
        result = tailor_resume(base, title, "the employer", jd_text, cfg=cfg,
                               guidance=guidance(report) + ("\n\n" + extra if extra else ""))
    except Exception as exc:
        # No model, or none answering: the draft is still made, from the
        # resume's own words, selected and laid out for the posting.
        result = TailorResult(summary=None)
        problem = (f"The writing model could not be used ({str(exc)[:80]}), so this draft uses the "
                   f"resume's own wording, selected and ordered for the posting.")
    draft = Draft(base, result, header, title, problem=problem)
    if not result.accepted:
        # A rejected rewrite is never used; the draft is the resume's own words,
        # selected and laid out for the posting.
        draft.accepted = False
        draft.problem = ("The rewrite claimed something the resume does not show, so it was not used. "
                         "This draft uses the resume's own wording, selected for the posting.")
        result = TailorResult(summary=None)
        draft.result = result
    if result.bullets:
        keep_required_wording(result, report)
    draft.model_omitted = list(result.omitted or [])
    if max_pages:
        try:
            fit_to_pages(base, result, jd_text, max_pages=max_pages,
                         pages=lambda b, r: _pages(b, r, header), keep=evidence_lines(report),
                         focus_terms=focus or None, proof=required_proof(report))
        except Exception:
            pass
    draft.changes = changes(base, result, header)
    return draft


def refit(draft: Draft, jd_text: str, report, focus: list[str], max_pages: int = 2) -> Draft:
    """The same rewrite fitted to the pages again, keeping the lines that carry `focus`.

    No model call: a term the resume has but the draft lost was usually cut for
    space ("platform" gone from a Platform Engineer resume, 94% -> 91%), and the
    line that carries it is already written. A whole new rewrite took 40 s to 5 min.
    """
    import copy

    from ml.resume.fit import fit_to_pages
    result = copy.deepcopy(draft.result)
    result.omitted = copy.deepcopy(draft.model_omitted)
    fit_to_pages(draft.base, result, jd_text, max_pages=max_pages,
                 pages=lambda b, r: _pages(b, r, draft.header), keep=evidence_lines(report),
                 focus_terms=focus or None, proof=required_proof(report))
    again = copy.copy(draft)
    again.result = result
    again.changes = changes(draft.base, result, draft.header)
    return again


def _pages(base, result, header) -> int:
    import tempfile
    from pathlib import Path

    from pypdf import PdfReader

    from ml.resume.pdf import write_pdf
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "measure.pdf"
        write_pdf(base, result, path, header_lines=header)
        return len(PdfReader(str(path)).pages)


def changes(base, result, header) -> list[dict]:
    """What the draft changed, for the Changes Made tab."""
    from ml.resume.writer import plan_document
    out: list[dict] = []
    for b in result.bullets or []:
        before, after = (b.get("original") or "").strip(), (b.get("tailored") or "").strip()
        if before and after and before != after:
            out.append({"kind": "Rewrote a bullet", "before": before, "after": after,
                        "why": (b.get("reason") or "").strip()})
    if result.summary:
        out.append({"kind": "Wrote the summary for this posting", "before": "", "after": result.summary,
                    "why": "A summary aimed at the posting, from the resume's own experience."})
    left = [(o.get("original") if isinstance(o, dict) else str(o)) or "" for o in result.omitted or []]
    left = [t for t in left if t]
    if left:
        # One entry with its lines listed: one change per dropped line buried the real edits.
        out.append({"kind": f"Left out {len(left)} line(s) less relevant to this posting",
                    "before": "\n".join(left), "after": "",
                    "why": "To fit the page limit, keeping what the posting needs most."})
    original = [s.heading for s in base.sections if s.heading != "HEADER"]
    drafted = [t for k, t in plan_document(base, result, header) if k == "heading"]
    order = [h for h in drafted if h in original]
    if order != [h for h in original if h in order]:
        out.append({"kind": "Reordered sections", "before": " → ".join(original),
                    "after": " → ".join(order), "why": "The most relevant sections first; education last."})
    return out
