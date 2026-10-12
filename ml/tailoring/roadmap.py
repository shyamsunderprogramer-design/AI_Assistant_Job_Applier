"""A skill roadmap: for each thing the posting wants that the resume does not show, how to close it.

A gap list says what is missing; it does not say what to do. For every gap
this answers, per skill:

  * what it is, and why this posting cares;
  * the bridge -- the closest thing the person has already done, QUOTED from
    their resume (checked: a quote not on the resume is dropped), and how it
    carries over. "You run EKS and AKS; OKE is Oracle's managed Kubernetes."
  * a plan in steps, each with a named free or official resource and hours;
  * one hands-on project that proves it, small enough to finish;
  * a certification, only where one is worth having;
  * what to say in an interview today, honestly: never "I have used it"
    when the resume says otherwise.

Nothing here is written into the resume. Learning it is the person's; the
roadmap only makes the path concrete. One model call per run, on request.
"""

from __future__ import annotations

import re

MAX_SKILLS = 8

ROADMAP_SYSTEM = """You are a career coach for a working engineer. The job below asks for skills their
resume does not show. For each skill listed under GAPS, write a short, practical roadmap.
Reply with JSON only:
{"skills": [{
   "skill": "the skill, in the job description's words",
   "what_it_is": "one sentence, plain words",
   "why_here": "one sentence: what this job uses it for, from the job description",
   "bridge": {"you_have": "a short quote copied word for word from the RESUME, or empty",
              "how_it_transfers": "one or two sentences: what carries over and what is new"},
   "plan": [{"step": "what to do", "resource": "a named official doc, free course or free tier", "hours": 0}],
   "project": "one hands-on project that proves the skill, finishable in a weekend or two",
   "certification": "a certification worth having for this skill, or empty",
   "interview_answer": "what to say if asked today, honest about where they stand"}]}
Rules:
- One entry per gap, in the order given. 3 to 5 plan steps each.
- you_have is copied exactly from the resume, or left empty. Never paraphrase it, never invent experience.
- interview_answer is what they can truthfully say TODAY, before doing any of the plan. It never claims
  they have used the skill unless the resume shows it, and never claims they are already learning,
  practising or building with it. Lead with the real adjacent experience from the resume, then say
  concretely how they would ramp up (the first things they would do), in the future tense.
- Name resources (e.g. "Oracle Cloud free tier", "Kubernetes docs: Tutorials"); do not make up URLs.
- hours are realistic for someone with this resume, not for a beginner."""


def gaps_of(report_matches: list[dict], missing_keywords: list[str] | None = None,
            limit: int = MAX_SKILLS) -> list[dict]:
    """The gaps worth a roadmap: required ones first, then preferred, then missing keywords.

    Each {skill, priority, status, evidence[]}: evidence is the matching step's
    own closest resume lines, so the bridge starts from the person's words.
    """
    out, seen = [], set()
    ranked = sorted((m for m in report_matches if m.get("status") in ("not_found", "unclear")),
                    key=lambda m: (m["requirement"].get("priority") != "required", m.get("status") != "not_found"))
    for m in ranked:
        req = m["requirement"]
        key = req.get("normalized") or req.get("original", "").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"skill": req.get("original", ""), "priority": req.get("priority", ""),
                    "status": m.get("status"), "context": req.get("context", ""),
                    "evidence": [e.get("text", "") for e in (m.get("evidence") or [])][:2]})
    for word in missing_keywords or []:
        if word.lower() not in seen:
            seen.add(word.lower())
            out.append({"skill": word, "priority": "keyword", "status": "not_found", "context": "", "evidence": []})
    return out[:limit]


# "I'm learning it", "I've been working through", "I'm currently building": a claim of
# progress the person may not have made. Shown with a warning, never silently.
_PROGRESS = re.compile(r"\b(i'?m|i am|i've been|i have been)\s+(now\s+|currently\s+|already\s+)?"
                       r"(learning|studying|building|working through|practi[cs]ing|applying|taking|preparing)",
                       re.I)


def claims_progress(answer: str) -> bool:
    return bool(_PROGRESS.search(answer or ""))


def _flat(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _on_resume(quote: str, resume_text: str) -> bool:
    """Is the quote really the person's words? Compared ignoring case and punctuation."""
    q = _flat(quote)
    return bool(q) and len(q) >= 8 and q in _flat(resume_text)


def build(resume_text: str, jd_text: str, gaps: list[dict], cfg, ask=None) -> list[dict]:
    """The roadmap, one entry per gap; bridges checked against the resume."""
    if not gaps:
        return []
    if ask is None:
        from ml.tailoring.review import _ask as ask
    listed = "\n".join(
        f"- {g['skill']} ({g['priority'] or 'asked'}"
        + (", partly shown" if g["status"] == "unclear" else "") + ")"
        + (f"; closest on the resume: {' | '.join(g['evidence'])}" if g["evidence"] else "")
        for g in gaps)
    body = ask(ROADMAP_SYSTEM, f"JOB DESCRIPTION:\n{jd_text[:8000]}\n\nGAPS:\n{listed}\n\nRESUME:\n{resume_text[:14000]}", cfg)
    out = []
    by_name = {g["skill"].lower(): g for g in gaps}
    for item in body.get("skills") or []:
        if not isinstance(item, dict) or not str(item.get("skill") or "").strip():
            continue
        bridge = item.get("bridge") if isinstance(item.get("bridge"), dict) else {}
        quote = str(bridge.get("you_have") or "").strip()
        if quote and not _on_resume(quote, resume_text):
            quote = ""                                   # not the person's words: dropped
        plan = []
        for step in item.get("plan") or []:
            if isinstance(step, dict) and str(step.get("step") or "").strip():
                try:
                    hours = max(0, min(200, int(round(float(step.get("hours") or 0)))))
                except (TypeError, ValueError):
                    hours = 0
                plan.append({"step": str(step["step"]).strip(), "resource": str(step.get("resource") or "").strip(),
                             "hours": hours})
        gap = by_name.get(str(item["skill"]).strip().lower(), {})
        out.append({
            "skill": str(item["skill"]).strip(),
            "priority": gap.get("priority", ""),
            "status": gap.get("status", ""),
            "what_it_is": str(item.get("what_it_is") or "").strip(),
            "why_here": str(item.get("why_here") or "").strip(),
            "you_have": quote,
            "how_it_transfers": str(bridge.get("how_it_transfers") or "").strip(),
            "plan": plan[:6],
            "hours": sum(s["hours"] for s in plan[:6]),
            "project": str(item.get("project") or "").strip(),
            "certification": str(item.get("certification") or "").strip(),
            "interview_answer": str(item.get("interview_answer") or "").strip(),
        })
        out[-1]["check_answer"] = claims_progress(out[-1]["interview_answer"])
    return out
