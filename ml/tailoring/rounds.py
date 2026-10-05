"""Write the draft again until it matches the posting well, and say how well.

One rewrite often matched the posting's keywords no better than the resume
it came from (53% -> 51% on a real posting), and nothing on the page said
so. Here each draft is measured the way an ATS reads it -- the keyword match
of its PDF text against the posting -- and a draft under the target is
written again, told which of the posting's terms the resume itself contains
and the draft left out. Up to three rounds; the best draft is kept, never a
worse one. The fabrication guard still checks every round.

Two numbers come back, each measured the same way on the original resume and
on the tailored one, so "before" and "after" can be compared honestly:
  ats           keyword match of the text (ml/resume/ats.py)
  requirements  the posting's requirements shown, by the matching rules
"""

from __future__ import annotations

import tempfile
from pathlib import Path

TARGET = 80
ROUNDS = 3

ALIGN = """# Round {round}: match the posting more closely
The last draft matched the posting's keywords {after}% (target {target}%).
- These terms are in the resume but missing from the draft. Bring each back where it
  truthfully fits: {fixable}
- Where the resume shows this kind of work in other words, describe it in the posting's
  terms: {concepts}
- Never add a skill, tool or claim the resume does not contain."""


def _pdf_text(draft) -> str:
    from ml.resume.ats import pdf_text
    from ml.resume.pdf import write_pdf
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "measure.pdf"
        write_pdf(draft.base, draft.result, path, header_lines=draft.header)
        return pdf_text(path)


def pages_of_original(draft) -> int:
    """Pages the whole original resume fills, in the same layout."""
    from ml.resume.fit import count_pages
    from ml.resume.tailor import TailorResult
    return count_pages(draft.base, TailorResult(summary=None))


def ats_of(draft, jd_text: str, company: str | None = None) -> dict:
    """The keyword match of the original resume and of this draft."""
    from ml.resume.ats import compare
    return compare(draft.base.text(), _pdf_text(draft), jd_text, company=company)


def requirements_shown(reqs, resume_file: Path) -> float:
    """The share of requirements a resume shows, by the matching rules alone (no model).

    Read from the document itself: PDF text loses the sections and job lines,
    and a tailored resume measured that way showed nothing at all (25% -> 0%).
    """
    from ml.tailoring import matching
    from ml.tailoring.reader import read_document
    from ml.tailoring.structure import build
    return matching.match(reqs, build(read_document(Path(resume_file))), None, use_model=False).score


def best_draft(make, jd_text: str, progress=lambda note: None, target: int = TARGET,
               rounds: int = ROUNDS, company: str | None = None):
    """Draft up to `rounds` times, aiming at `target`; returns (best draft, its ATS, rounds used).

    `make(extra_guidance, focus_terms)` writes one draft; focus terms are kept
    when the draft is fitted to its pages. The goal is the target or the
    original resume's own match, whichever is higher: stopping at 80% let a
    92% resume come back at 85%.
    """
    best, best_ats, used = None, None, 0
    extra, focus, goal = "", [], target
    for n in range(1, rounds + 1):
        if n > 1:
            progress(f"round {n} of {rounds}: aiming for {goal}%")
        draft = make(extra, focus)
        used = n
        ats = ats_of(draft, jd_text, company)
        goal = max(target, ats["before"] or 0)
        if best_ats is None or (ats["after"] or 0) > (best_ats["after"] or 0):
            best, best_ats = draft, ats
        if (best_ats["after"] or 0) >= goal:
            break
        if not draft.accepted and n > 1:
            break                      # the guard turned the rewrite down; another will too
        if not ats["fixable"] and not ats["concepts"]:
            break                      # nothing the resume can honestly add
        focus = list(dict.fromkeys(focus + ats["fixable"]))
        extra = ALIGN.format(round=n + 1, after=ats["after"], target=goal,
                             fixable=", ".join(ats["fixable"][:20]) or "(none)",
                             concepts=", ".join(ats["concepts"][:12]) or "(none)")
    return best, best_ats, used
