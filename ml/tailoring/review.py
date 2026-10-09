"""A recruiter's read of the resume, before and after tailoring.

Two model calls, each one JSON answer:

  * `recruiter_review` -- the original resume against the posting, read as a
    senior recruiter at that company would: a score out of 100 with its
    breakdown, the five most important missing keywords, and the three red
    flags a hiring manager sees in ten seconds. The keywords and red flags go
    into the rewrite, which still may claim only what the resume shows.
  * `skim_test` -- the tailored resume, read as an ATS and as a hiring
    manager on their 200th resume: which sections get skipped and why, a
    rewrite of each from the resume's own facts, and ATS problems.

Plus `needs_numbers`, no model: the bullets with no figure in them, which the
XYZ formula ("accomplished X, measured by Y, by doing Z") cannot be honestly
applied to until the person supplies the Y.

Both reviews are advice shown beside the draft. Neither failing stops a
tailoring run, and the score is a judgement, not the employer's ATS.
"""

from __future__ import annotations

import re

REVIEW_SYSTEM = """You are a senior recruiter at {company}, hiring for the job below. You read the
candidate's resume against the job description the way you do every day.

Reply with JSON only:
{{"score": 0-100,
  "breakdown": {{"skills": 0-25, "experience_level": 0-25, "keywords": 0-25, "impact": 0-25}},
  "missing_keywords": ["five keywords or skills the job description names that the resume does not show"],
  "red_flags": [{{"flag": "what a hiring manager notices in under 10 seconds", "fix": "how to fix it honestly"}}]}}

Rules:
- score is the sum of the breakdown. impact = how much of the work is shown with results and numbers.
- missing_keywords: exactly 5, most important first, each in the job description's own words.
  Only things the job description asks for; never something the resume already shows.
- red_flags: exactly 3, the ones that cost the interview, most serious first. Concrete
  ("no Kubernetes in the last role", "summary is generic"), never vague ("could be stronger").
- A fix never tells the candidate to claim what they have not done. If the honest fix is
  "state it only if you have done it", say so."""

SKIM_SYSTEM = """You are two readers at once: an applicant tracking system, and a hiring manager
reading 200 resumes in one sitting, giving each about six seconds.

Read the candidate's tailored resume for the job below and reply with JSON only:
{"skipped": [{"section": "section name", "why": "why the eye passes over it",
              "rewrite": "the section rewritten so it holds attention in six seconds"}],
 "ats_problems": ["formatting, headings or keyword problems an ATS would trip on"],
 "first_six_seconds": "one sentence: what the hiring manager takes away from the top third"}

Rules:
- List only sections that really get skipped; an empty list is a fine answer.
- A rewrite uses ONLY facts, tools and numbers already in this resume. Never add a number,
  tool, employer, title or result. Plain verbs, no buzzwords, no long dashes.
- Keep each rewrite no longer than the section it replaces."""


def _ask(system: str, user: str, cfg) -> dict:
    from ml.resume.llm import complete, extract_json
    reply = complete(system, user, cfg, max_tokens=3000)
    body = extract_json(reply.text)
    if not isinstance(body, dict):
        raise RuntimeError("the model did not answer in JSON")
    return body


def _clip(n, top: int) -> int:
    try:
        return max(0, min(top, int(round(float(n)))))
    except (TypeError, ValueError):
        return 0


def recruiter_review(resume_text: str, jd_text: str, company: str | None, cfg) -> dict:
    """{score, breakdown, missing_keywords[5], red_flags[3] of {flag, fix}}."""
    body = _ask(REVIEW_SYSTEM.format(company=company or "the hiring company"),
                f"JOB DESCRIPTION:\n{jd_text[:9000]}\n\nRESUME:\n{resume_text[:16000]}", cfg)
    parts = body.get("breakdown") or {}
    breakdown = {k: _clip(parts.get(k), 25) for k in ("skills", "experience_level", "keywords", "impact")}
    resume_low = resume_text.lower()
    missing = [str(k).strip() for k in body.get("missing_keywords") or [] if str(k).strip()]
    # A "missing" keyword the resume plainly has is the model misreading: dropped.
    missing = [k for k in missing if k.lower() not in resume_low][:5]
    flags = []
    for f in body.get("red_flags") or []:
        if isinstance(f, dict) and str(f.get("flag") or "").strip():
            flags.append({"flag": str(f["flag"]).strip(), "fix": str(f.get("fix") or "").strip()})
        elif isinstance(f, str) and f.strip():
            flags.append({"flag": f.strip(), "fix": ""})
    return {"score": sum(breakdown.values()) if any(breakdown.values()) else _clip(body.get("score"), 100),
            "breakdown": breakdown, "missing_keywords": missing, "red_flags": flags[:3]}


def skim_test(resume_text: str, jd_text: str, cfg) -> dict:
    """{skipped[{section, why, rewrite, unsupported_numbers}], ats_problems[], first_six_seconds}."""
    from backend.apply.essays import numbers_not_on
    body = _ask(SKIM_SYSTEM, f"JOB DESCRIPTION:\n{jd_text[:9000]}\n\nTAILORED RESUME:\n{resume_text[:16000]}", cfg)
    skipped = []
    for s in body.get("skipped") or []:
        if not isinstance(s, dict) or not str(s.get("section") or "").strip():
            continue
        rewrite = str(s.get("rewrite") or "").strip()
        skipped.append({"section": str(s["section"]).strip(), "why": str(s.get("why") or "").strip(),
                        "rewrite": rewrite, "unsupported_numbers": numbers_not_on(rewrite, resume_text)})
    return {"skipped": skipped[:6],
            "ats_problems": [str(p).strip() for p in body.get("ats_problems") or [] if str(p).strip()][:6],
            "first_six_seconds": str(body.get("first_six_seconds") or "").strip()}


def guidance(review: dict) -> str:
    """The review, as instructions for the rewrite. Keywords only where true."""
    if not review:
        return ""
    lines = ["A senior recruiter at this company reviewed the ORIGINAL resume."]
    if review.get("missing_keywords"):
        lines.append("Keywords the recruiter found missing. Use one ONLY where the resume already shows "
                     "that work under another name (an honest swap); otherwise leave it out: "
                     + "; ".join(review["missing_keywords"]))
    if review.get("red_flags"):
        lines.append("Red flags to remove, without claiming anything new:")
        lines += [f"- {f['flag']}" + (f" (fix: {f['fix']})" if f.get("fix") else "") for f in review["red_flags"]]
    return "\n".join(lines)


NUMBER = re.compile(r"\d")


def needs_numbers(bullets: list[str]) -> list[str]:
    """Bullets with no figure: the XYZ formula needs a measure only the person can give."""
    lines = [re.sub(r"^[\s•\u2002\-*]+", "", b or "").strip() for b in bullets]
    # A sentence, not a heading: "Infrastructure as Code (IaC) & Cloud Provisioning" is a heading.
    return [b for b in lines if b and b.endswith((".", "!")) and not NUMBER.search(b)]
