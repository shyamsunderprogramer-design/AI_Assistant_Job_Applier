"""Resume tailoring, by whichever model `resume/llm.py` is pointed at.

Hard rule (README.md §C8): the model may only rephrase, reorder, and
re-emphasise content already present in the base resume. Every output is run
through `resume.guard` before it is written to disk; a tailoring that invents
anything is rejected, not silently used.

That rule is why the backend is swappable at all. The guard does not trust the
model — it re-checks every skill, metric, year and entity against the base
resume regardless of what produced the text. So a 27B model running on this
laptop is held to exactly the same standard as a frontier model behind an API,
and the only thing that changes when you switch is the bill.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from resume.cost import CallCost, Ledger, record_usage
from resume.guard import GuardResult, check_no_fabrication
from resume.llm import ProviderUnavailable, complete, extract_json, model_name
from resume.parser import Resume

log = logging.getLogger(__name__)

# Kept for callers and tests that still name it; the model actually used comes
# from config via `resume.llm.model_name`.
MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You tailor an existing resume to a specific job description.

ABSOLUTE RULE — you may not invent anything. You may ONLY:
  - rephrase existing bullets using the job description's vocabulary
  - reorder bullets and skills so the most relevant appear first
  - drop bullets that are irrelevant to this role
  - re-emphasise real accomplishments already in the resume

You may NEVER:
  - add a skill, tool, language, or technology not already in the resume
  - add or alter an employer, job title, date, degree, certification, or metric
  - invent numbers, percentages, team sizes, or outcomes
  - imply seniority or scope the resume does not support

If the resume genuinely lacks something the job asks for, leave it out. A
truthful weaker match is the correct output — the human will decide whether to
apply. Never bridge a gap by inventing.

Return ONLY a JSON object, no prose, in this exact shape:
{
  "summary": "<2-3 sentence professional summary, or null if the base resume has none>",
  "bullets": [
    {"original": "<the base-resume bullet you rewrote, verbatim>",
     "tailored": "<your rewrite>",
     "reason": "<why this bullet matters for this JD, one clause>"}
  ],
  "skills_order": ["<the resume's own skills, reordered by relevance>"],
  "omitted": ["<base bullets you left out and why, one clause each>"],
  "gaps": ["<what the JD asks for that the resume genuinely lacks>"]
}

"gaps" is important — be honest there. It is how the human learns whether to
apply at all."""


@dataclass
class TailorResult:
    summary: str | None
    bullets: list[dict[str, str]] = field(default_factory=list)
    skills_order: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    guard: GuardResult | None = None
    raw_response: str = ""
    cost: CallCost | None = None

    @property
    def accepted(self) -> bool:
        return self.guard is None or self.guard.ok

    def tailored_text(self) -> str:
        """Everything Claude generated, for the fabrication guard to inspect."""
        parts = [self.summary or ""]
        parts.extend(b.get("tailored", "") for b in self.bullets)
        parts.extend(self.skills_order)
        return "\n".join(p for p in parts if p)


def build_prompt(resume: Resume, job_title: str, company: str, jd_text: str) -> str:
    """The user prompt for one tailoring call.

    Exposed so the cost estimator can measure the real prompt rather than a
    guess at its size.
    """
    return (
        f"# Target role\n{job_title} at {company}\n\n"
        f"# Job description\n{jd_text.strip()}\n\n"
        f"# My current resume (the ONLY source of truth about me)\n{resume.text().strip()}"
    )


def tailor_resume(
    resume: Resume,
    job_title: str,
    company: str,
    jd_text: str,
    client=None,
    ledger: Ledger | None = None,
    cfg=None,
) -> TailorResult:
    """Tailor the resume with the configured model, then verify it invented nothing.

    `client` is accepted and ignored except in tests that inject a fake; which
    backend runs is a config decision (`resume.provider`), not a call-site one,
    so that a daily run and a manual run can never disagree about it.
    """
    user_prompt = build_prompt(resume, job_title, company, jd_text)
    answer = complete(SYSTEM_PROMPT, user_prompt, cfg, max_tokens=16000)

    cost = None
    if ledger is not None and answer.usage is not None:
        cost = record_usage(ledger, answer.model, answer.usage, cfg)
        log.info("Tailoring cost for %s at %s: %s", job_title, company, cost)
    elif answer.free:
        # Nothing to meter, and saying so beats a blank where a price was.
        log.info("Tailored %s at %s with %s locally — no cost",
                 job_title, company, answer.model)

    text = answer.text
    payload = extract_json(text)

    result = TailorResult(
        summary=payload.get("summary"),
        bullets=[b for b in payload.get("bullets", []) if isinstance(b, dict)],
        skills_order=[str(s) for s in payload.get("skills_order", [])],
        omitted=[str(s) for s in payload.get("omitted", [])],
        gaps=[str(s) for s in payload.get("gaps", [])],
        raw_response=text,
        cost=cost,
    )

    result.guard = check_no_fabrication(resume.text(), result.tailored_text())
    if not result.guard.ok:
        log.warning(
            "Fabrication guard rejected tailoring for %s at %s:\n%s",
            job_title, company, result.guard.report(),
        )

    return result
