"""Resume tailoring via the Claude API.

Hard rule (README.md §C8): Claude may only rephrase, reorder, and
re-emphasise content already present in the base resume. Every output is run
through `resume.guard` before it is written to disk; a tailoring that invents
anything is rejected, not silently used.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

import anthropic

from resume.guard import GuardResult, check_no_fabrication
from resume.parser import Resume

log = logging.getLogger(__name__)

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

    @property
    def accepted(self) -> bool:
        return self.guard is None or self.guard.ok

    def tailored_text(self) -> str:
        """Everything Claude generated, for the fabrication guard to inspect."""
        parts = [self.summary or ""]
        parts.extend(b.get("tailored", "") for b in self.bullets)
        parts.extend(self.skills_order)
        return "\n".join(p for p in parts if p)


def _client() -> anthropic.Anthropic:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env before running `tailor`."
        )
    return anthropic.Anthropic()


def _extract_json(text: str) -> dict:
    """Parse the model's JSON, tolerating a stray code fence."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in the model response")
    return json.loads(cleaned[start : end + 1])


def tailor_resume(
    resume: Resume,
    job_title: str,
    company: str,
    jd_text: str,
    client: anthropic.Anthropic | None = None,
) -> TailorResult:
    """Ask Claude to tailor the resume, then verify it invented nothing."""
    client = client or _client()

    user_prompt = (
        f"# Target role\n{job_title} at {company}\n\n"
        f"# Job description\n{jd_text.strip()}\n\n"
        f"# My current resume (the ONLY source of truth about me)\n{resume.text().strip()}"
    )

    # Streaming: JD + resume + reasoning can be long, and streaming avoids
    # HTTP timeouts on a big max_tokens.
    with client.messages.stream(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError(f"Model declined the request: {response.stop_details}")

    text = "".join(block.text for block in response.content if block.type == "text")
    payload = _extract_json(text)

    result = TailorResult(
        summary=payload.get("summary"),
        bullets=[b for b in payload.get("bullets", []) if isinstance(b, dict)],
        skills_order=[str(s) for s in payload.get("skills_order", [])],
        omitted=[str(s) for s in payload.get("omitted", [])],
        gaps=[str(s) for s in payload.get("gaps", [])],
        raw_response=text,
    )

    result.guard = check_no_fabrication(resume.text(), result.tailored_text())
    if not result.guard.ok:
        log.warning(
            "Fabrication guard rejected tailoring for %s at %s:\n%s",
            job_title, company, result.guard.report(),
        )

    return result
