"""Cover letters, under the same fabrication guard as the resume.

A letter is prose about you addressed to a named company, which makes the
resume guard almost right and wrong in one specific way: it flags capitalised
words the resume never mentions, and a cover letter names the hiring company by
definition. Every letter would be rejected for saying "ClickUp".

The fix is not to loosen the check but to say precisely what it is for. The
guard protects against inventing facts ABOUT YOU:

  skill / metric / year   judged against the RESUME alone. A letter claiming
                          ten years of Kubernetes is a fabrication even when
                          the posting asks for ten — what an employer wants is
                          not evidence that you have it.
  entity                  also allows nouns from the JOB DESCRIPTION. Naming
                          the company, its product or its stack is quoting the
                          employer back, not a claim about your history.

`guard.py` is left untouched so resume tailoring keeps the stricter rule.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from resume.guard import GuardResult, Violation, check_no_fabrication
from resume.parser import Resume

log = logging.getLogger(__name__)

MAX_WORDS = 320

SYSTEM_PROMPT = """You write a cover letter for one job application.

The resume you are given is the ONLY source of truth about the candidate. You
may select from it, summarise it and connect it to the posting. You may NOT
add a skill, a number, a date, an employer or an achievement that is not in
the resume — not even one the job description asks for. If the candidate does
not have something the posting wants, leave it out; do not imply it.

Write plainly. No "I am writing to express my interest", no "passionate", no
"synergy", no "I would be thrilled". Contractions are fine. Short sentences
are better than long ones. Under 320 words in total — a letter a tired person
reads at the end of a long day.

Three paragraphs, each with a job to do:

  1. WHY THIS ROLE. Name something specific from the posting — a system, a
     problem, a scale, a constraint — and connect it to work the candidate has
     actually done. Never "I was excited to see your posting". This paragraph
     is the only reason anyone reads the second one.

  2. THE EVIDENCE. One or two concrete things from the resume that bear
     directly on paragraph one: what was built, at what scale, with what
     result. Numbers if the resume has them; no numbers if it does not.
     Specific beats comprehensive — this is not a summary of the resume,
     which is attached anyway.

  3. THE CLOSE. Short. What the candidate would work on, or what they want to
     talk about. No restating, no "I look forward to hearing from you at your
     earliest convenience".

If the resume does not support a claim the posting asks for, leave it out of
the letter entirely and name it in "left_out" instead. A letter that quietly
skips a gap is honest; one that implies the gap is filled is not.

Reply with JSON only:

{
  "greeting": "Dear Hiring Manager,  (or a name if the posting gives one)",
  "body": [
    "why this role — specific to the posting, tied to real work",
    "the evidence — concrete, from the resume, relevant to paragraph one",
    "the close — short, forward-looking"
  ],
  "closing": "Sincerely,\\nName",
  "why_this_company": "one sentence on what in the posting prompted the letter",
  "left_out": ["anything the posting wants that the resume does not support"]
}
"""


@dataclass
class LetterResult:
    """A drafted letter plus the verdict on whether it invented anything."""

    greeting: str = ""
    body: list[str] = field(default_factory=list)
    closing: str = ""
    why_this_company: str = ""
    left_out: list[str] = field(default_factory=list)
    guard: GuardResult | None = None

    @property
    def accepted(self) -> bool:
        # Deliberately fail-CLOSED, unlike TailorResult: an unguarded letter is
        # prose in the candidate's name and must never reach disk unchecked.
        return self.guard is not None and self.guard.ok

    def claim_text(self) -> str:
        """The part that makes claims, and therefore the part that is guarded.

        `left_out` is commentary about what was omitted — guarding it would
        flag the very skills the letter correctly refused to claim.
        """
        return "\n".join([*self.body, self.why_this_company])

    def text(self) -> str:
        parts = [self.greeting, "", *self.body, "", self.closing]
        return "\n\n".join(p for p in parts if p is not None).strip() + "\n"

    def word_count(self) -> int:
        return len(" ".join(self.body).split())


def build_prompt(resume: Resume, job_title: str, company: str, jd_text: str) -> str:
    """Same three-section shape as the resume prompt, so both read alike."""
    return (
        f"# Target role\n{job_title} at {company}\n\n"
        f"# Job description\n{(jd_text or '').strip()}\n\n"
        f"# My current resume (the ONLY source of truth about me)\n"
        f"{resume.text().strip()}\n"
    )


def parse_reply(text: str) -> dict:
    """Read a model's JSON reply, liberally on wrapping, strictly on contents."""
    if not (text or "").strip():
        raise ValueError("The reply file is empty.")

    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    payload = fenced.group(1) if fenced else text
    start, end = payload.find("{"), payload.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in the reply.")

    try:
        data = json.loads(payload[start: end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"The reply is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("The reply must be a JSON object.")
    if "body" not in data:
        raise ValueError('The reply has no "body" — nothing to write.')
    return data


def check_letter(resume_text: str, jd_text: str, letter_text: str) -> GuardResult:
    """Guard a letter: claims against the resume, names against the posting.

    Runs the ordinary check first, then forgives exactly one class of
    violation — an `entity` that the job description itself contains.
    """
    verdict = check_no_fabrication(resume_text, letter_text)
    if verdict.ok:
        return verdict

    jd_vocabulary = _vocabulary(jd_text)
    kept: list[Violation] = [
        violation for violation in verdict.violations
        if not (violation.kind == "entity" and _from_posting(violation.value, jd_vocabulary))
    ]
    return GuardResult(ok=not kept, violations=kept)


def _vocabulary(text: str) -> set[str]:
    """Every word the posting uses, stripped of the punctuation stuck to it.

    Raw tokens are not enough. A real posting writes "FedRAMP®" and "FedRAMP,"
    and never the bare word, so a letter saying "FedRAMP" matched nothing and
    was rejected for quoting the posting it was answering. Hyphenated
    compounds are split too, and kept whole as well.
    """
    vocabulary: set[str] = set()
    for word in _words(text):
        cleaned = word.strip(".,;:!?()[]{}\"'“”‘’®™©-").lower()
        if not cleaned:
            continue
        vocabulary.add(cleaned)
        vocabulary.update(part for part in cleaned.split("-") if part)
    return vocabulary


def _from_posting(value: str, vocabulary: set[str]) -> bool:
    """Is this name the posting's own word, rather than a claim about anyone?

    A model writing about the employer composes phrases the posting implies
    but never spells: "FedRAMP-authorized" out of a posting that says only
    "FedRAMP®". Every part of the compound has to come from the posting, so
    an invented word cannot ride in attached to a real one.
    """
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return False
    if cleaned in vocabulary:
        return True
    parts = [part for part in re.split(r"[-\s/]+", cleaned) if part]
    return bool(parts) and all(part in vocabulary for part in parts)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9&.\-]+", text or "")


def result_from_reply(resume: Resume, jd_text: str, reply_text: str) -> LetterResult:
    """Build a guarded LetterResult from a model's reply."""
    data = parse_reply(reply_text)
    body = [str(p).strip() for p in (data.get("body") or []) if str(p).strip()]
    result = LetterResult(
        greeting=str(data.get("greeting") or "Dear Hiring Manager,").strip(),
        body=body,
        closing=str(data.get("closing") or "Sincerely,").strip(),
        why_this_company=str(data.get("why_this_company") or "").strip(),
        left_out=[str(g).strip() for g in (data.get("left_out") or []) if str(g).strip()],
    )
    result.guard = check_letter(resume.text(), jd_text, result.claim_text())
    if not result.guard.ok:
        log.warning("Cover letter rejected: %s", result.guard.report())
    return result
