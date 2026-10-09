"""Draft answers to an application's essay questions, from the person's own resume.

"Walk us through the Kubernetes clusters you operated" deserves a specific,
confident answer -- and the employer will read it beside the tailored resume
sent with the same application, then ask about it in the interview. So the
draft is written from the resume (the full master, which holds more detail
than the two pages sent) and the posting, in the posting's language, and
claims nothing the resume does not show:

  * every number in the draft must appear on the resume, or it is flagged;
  * what the question asks that the resume does not show is listed as a gap,
    for the person to fill in or leave out.

A draft is never sent unread: it is kept beside the question until the
person edits it and presses Save.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

ESSAY = re.compile(r"\b(describe|walk us|tell us|explain|share|how have you|how do you|what did you|example|why do you|"
                   r"why are you|what interests|what excites|cover letter|additional information)\b", re.I)

SYSTEM = """You write a job candidate's answer to one essay question on a job application.

You get the candidate's resume, the job posting, and the question. Write the answer the candidate
would submit: first person, confident and specific, in the posting's own vocabulary where it
truthfully fits. Lead with the strongest relevant fact, then what they owned, what they did, and
the result. 120 to 220 words, plain paragraphs, no headings, no bullet lists, no greeting.

Hard rules:
- Use ONLY facts stated in the resume: employers, tools, scale, numbers, outcomes. Never invent a
  tool, number, employer, team size, certification or result.
- If the question asks about something the resume does not show, do not claim it: answer with the
  closest real experience, stated positively, and list what is missing under "gaps".
- The answer is what the candidate submits, so it never mentions "the resume", never apologises,
  and never says what the candidate cannot claim, has not done or cannot quantify. No hedging
  sentences ("this reflects broader scope", "I cannot point to"). Missing things go ONLY in "gaps".
- Pick the 3-5 strongest facts for THIS question; do not list every tool. End on a concrete result.
- Never name a company the resume does not name.

Reply with JSON only: {"answer": "...", "gaps": ["what the question asks that the resume does not show", ...]}"""


def is_essay(label: str) -> bool:
    """A question that wants a written answer, not a choice or a short fact."""
    return len(label or "") > 40 and bool(ESSAY.search(label or ""))


def resume_text(cfg) -> str:
    from ml.resume.pipeline import load_base_resume
    return load_base_resume(cfg).text()


def numbers_not_on(text: str, resume: str) -> list[str]:
    """Numbers in the draft that the resume never states (years and small counts included)."""
    on_resume = set(re.findall(r"\d+(?:\.\d+)?", resume or ""))
    return sorted({n for n in re.findall(r"\d+(?:\.\d+)?", text or "") if n not in on_resume})


def draft(cfg, job, label: str, resume: str | None = None) -> dict:
    """{answer, gaps, unsupported_numbers} for one essay question on one job."""
    from ml.resume.llm import complete, extract_json
    resume = resume if resume is not None else resume_text(cfg)
    user = (f"JOB: {job.title} at {job.company}\n\nPOSTING:\n{(job.description or '')[:9000]}\n\n"
            f"CANDIDATE RESUME:\n{resume[:16000]}\n\nQUESTION:\n{label}")
    reply = complete(SYSTEM, user, cfg, max_tokens=2000)
    body = extract_json(reply.text)
    answer = " ".join(str(body.get("answer") or "").split()) if "\n\n" not in str(body.get("answer") or "") \
        else str(body.get("answer")).strip()
    if not answer:
        raise RuntimeError("The model returned no answer; try again.")
    return {"answer": answer, "gaps": [str(g) for g in body.get("gaps") or []][:6],
            "unsupported_numbers": numbers_not_on(answer, resume),
            "hedges": hedges(answer)}


HEDGE = re.compile(r"\b(my resume|the resume|i cannot|i can't|i do not have|i don't have|not documented|"
                   r"does not (establish|document|show)|i would distinguish)\b", re.I)


def hedges(answer: str) -> list[str]:
    """Sentences that talk about the resume or what the candidate cannot claim: never sent as is."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", answer or "") if HEDGE.search(s)]


def draft_for_application(cfg, job_id: int, labels: list[str] | None = None, say=print) -> dict:
    """Draft every essay question still open on one prepared application; kept as drafts, never as answers."""
    from backend.apply.prepare import _save, load
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session
    data = load()
    entry = data.get(str(job_id))
    if entry is None:
        raise ValueError(f"Job {job_id} is not a prepared application.")
    with get_session() as session:
        job = session.get(Job, job_id)
        session.expunge(job)
    resume = resume_text(cfg)
    wanted = [q["label"] for q in entry.get("questions") or [] if is_essay(q["label"])
              and (labels is None or q["label"] in labels)]
    drafts = entry.setdefault("drafts", {})
    for label in wanted:
        say(f"  drafting: {label[:70]}…")
        try:
            drafts[label] = {**draft(cfg, job, label, resume),
                             "made": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        except Exception as exc:
            say(f"    could not draft ({str(exc)[:100]})")
    data = load()                          # the file may have changed while the model wrote
    data[str(job_id)].setdefault("drafts", {}).update(drafts)
    _save(data)
    return drafts
