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

# Every term the resume can truthfully show, not 80%: the person asked for a 100% match
# (21 Oct 2026). Rounds still stop when nothing the resume has is left to bring back or
# reword, so a posting asking for tools the person never used ends below 100 -- by design.
TARGET = 100
ROUNDS = 3

# The resume is the only evidence (30 Oct 2026). On 22 Oct the last rule here was rewritten into
# "Add a skill if not already present, tool or claim the resume does not contain", the opposite
# of the rule it replaced, and every round after the first was sent it (audit RT-01). A skill the
# uploaded resume lacks counts only when the person added it from a job page and ticked that it is
# true (ml/resume/additions.py): it is then part of the resume, so `fixable` already holds it.
ALIGN = """# Round {round}: match the posting more closely
The last draft matched the posting's keywords {after}% (target {target}%).
- These terms are in the resume but missing from the draft. Bring each back where it
  truthfully fits: {fixable}
- The posting's words for kinds of work the resume never names. For each, find a line whose
  own work is that work and describe it with the term; if none is, leave it out: {concepts}
- The resume, with the lines the person added and confirmed, is the only evidence. Never add a
  skill, tool, certification, experience, employer, title, date, duty, achievement or number it
  does not contain: the posting asking for one is not evidence the person has it.
- Never present related or similar work as direct experience of what the posting names.
- What the resume does not show stays a gap. A match below the target is then the honest
  result; never add words to reach it."""


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
               rounds: int = ROUNDS, company: str | None = None, refit=None):
    """Draft up to `rounds` times, aiming at `target`; returns (best draft, its ATS, rounds used).

    `make(extra_guidance, focus_terms)` writes one draft; focus terms are kept
    when the draft is fitted to its pages. The goal is the target or the
    original resume's own match, whichever is higher: stopping at 80% let a
    92% resume come back at 85%.

    `refit(draft, focus)` fits the same rewrite to the pages again, keeping the
    lines with the terms it lost. It costs no model call, so it is tried before
    every new round, and a round is only written when it falls short.
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
        if refit is not None and ats["fixable"]:
            progress(f"keeping the lines with {', '.join(ats['fixable'][:4])}")
            try:
                again = refit(draft, list(dict.fromkeys(focus + ats["fixable"])))
                again_ats = ats_of(again, jd_text, company)
            except Exception:
                again, again_ats = None, None
            if again_ats and (again_ats["after"] or 0) > (best_ats["after"] or 0):
                best, best_ats = again, again_ats
                ats = again_ats
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
