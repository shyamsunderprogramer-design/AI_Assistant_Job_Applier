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
import re
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

HOW TO REWRITE A BULLET
This is a resume a recruiter reads in six seconds, not an argument that the
candidate fits. Four rules, and they matter more than the rewrite being clever:

1. KEEP THE TENSE. A bullet written in present tense belongs to the job the
   person still holds. Changing "Analyze" to "Analyzed" tells the reader they
   have left. Never change the tense of a verb.

2. NEVER DELETE A NUMBER. Every figure in the original -- a percentage, a
   count, a duration, a dollar amount, "99.95%", "200+", "38% faster" -- must
   appear in your rewrite, unchanged. Numbers are the strongest thing on a
   resume and the most common reason a recruiter calls. Measured before this
   rule existed: 61% of them were being deleted, because shortening a bullet
   by cutting its result clause is the easiest way to shorten it and the
   worst. If a bullet must lose something, lose an adjective.

3. SAME LENGTH OR SHORTER, AFTER RULE 2. You are swapping words, not adding
   them -- but never buy brevity with a metric. A rewrite that is two words
   longer and keeps "99.98% uptime" is better than a short one that drops it.

4. NEVER EXPLAIN THE RELEVANCE INSIDE THE BULLET. Do not append clauses like
   "directly supporting cost control", "mirroring the collaboration this role
   requires", or "demonstrating ability to resolve shortages". A resume
   describes the work; it never argues about the application. Put that
   reasoning in the "reason" field, which is where the human reads it.

5. REPLACE, DON'T APPEND. Use the job description's vocabulary by swapping it
   for the resume's own synonym, inside the sentence — not by bolting a phrase
   onto the end. If the resume says "purchase orders" and the JD says "POs",
   that is a legitimate swap. If there is no honest swap to make, return the
   bullet unchanged: an unchanged bullet is a perfectly good answer and far
   better than a padded one.

  Original : Built observability with Prometheus and Grafana, achieving 38%
             faster MTTR and proactive anomaly detection.
  BAD      : Built observability with Prometheus and Grafana, achieving
             proactive issue detection and faster MTTR.
             (shorter, and it deleted the 38% -- the best part of the bullet)
  GOOD     : Built observability with Prometheus, Grafana and Datadog,
             achieving 38% faster MTTR and proactive anomaly detection.
             (same length, metric intact, JD's own tool named)

  Original : Analyze purchase requirements, inventory levels, and demand
             forecasts to support procurement and planning decisions.
  BAD      : Analyzed purchase requirements, inventory levels, and demand
             forecasts to support procurement and planning decisions, directly
             supporting material availability and cost control.
             (tense changed, longer, and the added clause argues the case)
  GOOD     : Analyze material buy lists, inventory levels, and demand
             forecasts to set purchasing priorities.
             (same tense, shorter, and the JD's own words replaced synonyms)

Do not end several bullets with the same phrase. Repetition across bullets
reads as a template and is worse than leaving them alone.

Return ONLY a JSON object, no prose, in this exact shape:
{
  "summary": "<2-3 sentence professional summary, or null if the base resume has none>",
  "bullets": [
    {"original": "<the base-resume bullet you rewrote, verbatim>",
     "tailored": "<your rewrite>",
     "reason": "<why this bullet matters for this JD, one clause>"}
  ],
  "skills_order": ["<the resume's own skills, reordered by relevance>"],
  "omitted": [
    {"original": "<the base-resume bullet you dropped, VERBATIM>",
     "reason": "<why it does not earn its place for this role, one clause>"}
  ],
  "gaps": ["<what the JD asks for that the resume genuinely lacks>"]
}

"omitted" is how the resume gets SHORTER. Tailoring is as much about what you
leave out as what you rephrase, and a resume that keeps everything for every
job is not tailored. Copy the dropped bullet VERBATIM into "original" -- a
description of what you dropped cannot be acted on, so a paraphrase means the
bullet stays in the document.

Drop a bullet when it is genuinely irrelevant to this posting. Never drop an
employer, a job title, a date, a degree, or a whole role -- those are the
person's record, not padding. Keep at least half of each job's bullets: a role
stripped to one line reads as a gap in the career, which is worse than a
slightly long resume.

"gaps" is important — be honest there. It is how the human learns whether to
apply at all."""


@dataclass
class TailorResult:
    summary: str | None
    bullets: list[dict[str, str]] = field(default_factory=list)
    skills_order: list[str] = field(default_factory=list)
    omitted: list[dict] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    guard: GuardResult | None = None
    raw_response: str = ""
    cost: CallCost | None = None
    retried: bool = False
    # What the first attempt claimed and the retry dropped. Kept so the review
    # note can show it: a near miss is worth seeing, not hiding.
    first_violations: list[str] = field(default_factory=list)

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
    result = _attempt(resume, user_prompt, cfg, ledger, job_title, company)

    # A rejected tailoring produces nothing the person can send, and the model
    # is usually wrong in a small, nameable way -- measured over five real
    # postings, three were held for lifting service names out of the job
    # description (GuardDuty, CloudTrail, Pulumi) into experience the resume
    # does not claim. Naming those terms and asking again fixes most of them,
    # so the alternative to one more call is handing back nothing.
    #
    # Once, never in a loop: a model that reintroduces a claim after being
    # told exactly which one to drop will keep doing it, and each attempt
    # costs real money on a paid provider.
    if not result.guard.ok and _retries_allowed(cfg):
        log.info("Retrying %s at %s without: %s", job_title, company,
                 ", ".join(_claims(result.guard)))
        retry = _attempt(resume, _retry_prompt(user_prompt, result.guard),
                         cfg, ledger, job_title, company)
        # Keep the retry only if it is genuinely cleaner. A second answer that
        # invented different things is not progress.
        if retry.guard.ok or len(retry.guard.violations) < len(result.guard.violations):
            retry.retried = True
            retry.first_violations = [str(v) for v in result.guard.violations]
            result = retry

    if not result.guard.ok:
        log.warning(
            "Fabrication guard rejected tailoring for %s at %s:\n%s",
            job_title, company, result.guard.report(),
        )
    elif getattr(result, "retried", False):
        log.info("Retry cleared the guard for %s at %s", job_title, company)

    return result


def _retries_allowed(cfg) -> bool:
    return bool(cfg is None or cfg.get("resume.retry_on_guard_rejection", True))


def _claims(guard) -> list[str]:
    """The distinct values the guard objected to, for the retry prompt."""
    return list(dict.fromkeys(v.value for v in guard.violations))


def _retry_prompt(original: str, guard) -> str:
    """The same task, with the rejected claims named.

    Naming them matters. "Do not invent anything" is what the first prompt
    already said and the model still wrote GuardDuty; "the resume never
    mentions GuardDuty" is a fact it can act on.
    """
    claims = _claims(guard)
    listed = "\n".join(f"  - {c}" for c in claims)
    return (
        f"{original}\n\n"
        f"# Your previous answer was rejected\n"
        f"It used these terms, and the resume above does not contain any of "
        f"them:\n{listed}\n\n"
        f"Every one of those came from the job description, not from this "
        f"person's experience. Write the tailoring again with all of them "
        f"removed. Do not substitute a synonym and do not describe the same "
        f"capability in other words -- if the resume does not evidence it, it "
        f"belongs in \"gaps\", not in a bullet. Everything else about your "
        f"previous answer was fine; change only what is listed."
    )


METRIC_RE = re.compile(r"\d+(?:\.\d+)?\s*%|\$\s?\d[\d,.]*|\b\d[\d,]{2,}\b|\b\d+(?:\.\d+)?x\b")


def _metrics(text: str) -> set[str]:
    """Every figure in a bullet: percentages, counts, money, multipliers."""
    return {m.group(0).replace(" ", "") for m in METRIC_RE.finditer(text or "")}


def _keep_metrics(bullets: list[dict]) -> int:
    """Restore any bullet whose rewrite dropped a number, and say how many.

    The prompt asks; this enforces. Measured before either existed, 61% of the
    figures in a resume were being deleted -- "99.98% uptime", "42% faster",
    "200+ workloads" -- because the quickest way to shorten a bullet is to cut
    its result clause, which is the half a recruiter actually reads.

    A rewrite that loses a metric is not a better bullet, so the original is
    kept instead. Losing the JD's vocabulary costs less than losing the proof.
    """
    restored = 0
    for bullet in bullets:
        before = bullet.get("original") or ""
        after = bullet.get("tailored") or ""
        if not before or not after:
            continue
        lost = _metrics(before) - _metrics(after)
        if lost:
            bullet["tailored"] = before
            bullet["reason"] = (f"kept as written — the rewrite dropped "
                                f"{', '.join(sorted(lost))}")
            restored += 1
    return restored


def _omissions(raw) -> list[dict]:
    """Normalise "omitted" to {original, reason} entries.

    It used to be a list of prose descriptions -- "the front-end technologies
    were omitted" -- which read well in the review note and could not be acted
    on, so nothing was ever dropped and every tailored resume came out the
    same length as the original. A bare string is still accepted, and still
    cannot drop anything; it is kept so the note can show it.
    """
    entries = []
    for item in raw or []:
        if isinstance(item, dict):
            entries.append({"original": str(item.get("original") or "").strip(),
                            "reason": str(item.get("reason") or "").strip()})
        elif str(item).strip():
            entries.append({"original": "", "reason": str(item).strip()})
    return entries


def _attempt(resume: Resume, user_prompt: str, cfg, ledger: Ledger | None,
             job_title: str, company: str) -> TailorResult:
    """One model call, parsed and guarded."""
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
        omitted=_omissions(payload.get("omitted")),
        gaps=[str(s) for s in payload.get("gaps", [])],
        raw_response=text,
        cost=cost,
    )
    restored = _keep_metrics(result.bullets)
    if restored:
        log.info("Kept %d bullet(s) as written: the rewrite dropped a metric",
                 restored)

    result.guard = check_no_fabrication(resume.text(), result.tailored_text())
    return result
