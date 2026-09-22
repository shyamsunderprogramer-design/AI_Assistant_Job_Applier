"""One retry when the guard rejects, and the rules around it.

A held tailoring gives the person nothing to send. Measured over five real
postings with gemma4:e4b, three were held for writing service names out of
the job description -- GuardDuty, CloudTrail, Pulumi, ChatOps -- into
experience the resume does not claim. Naming those terms and asking again
cleared most of them.

The dangerous version of this feature is a loop: a model that reintroduces a
claim after being told exactly which one to drop will keep doing it, and on a
paid provider every attempt is billed. So: once, and only kept if better.
"""

import json

import pytest

from resume.parser import Resume, Section
from resume.tailor import tailor_resume

# raw_text is what the guard reads -- Resume.text() returns it, not the
# sections -- so an empty one would flag every word in the answer.
BASE_TEXT = (
    "Jane Roe\njane@example.com\n"
    "EXPERIENCE\n"
    "Ran Kubernetes clusters on AWS with Terraform and Prometheus.\n"
)
BASE = Resume(raw_text=BASE_TEXT, sections=[
    Section(heading="HEADER", lines=["Jane Roe", "jane@example.com"]),
    Section(heading="EXPERIENCE", lines=[
        "Ran Kubernetes clusters on AWS with Terraform and Prometheus.",
    ]),
])


def answer(bullet: str) -> str:
    return json.dumps({
        "summary": None,
        "bullets": [{"original": "Ran Kubernetes clusters on AWS with Terraform and Prometheus.",
                     "tailored": bullet, "reason": "relevant"}],
        "skills_order": [], "omitted": [], "gaps": [],
    })


CLEAN = "Ran Kubernetes clusters on AWS with Terraform."
INVENTED = "Ran Kubernetes on AWS with GuardDuty and Pulumi."
WORSE = "Ran Kubernetes on AWS with GuardDuty, Pulumi and CloudTrail."


def replies(monkeypatch, *texts):
    """Queue model answers; record the prompt each call received."""
    seen = []

    def fake(system, user, cfg=None, **kwargs):
        from resume.llm import Completion
        seen.append(user)
        return Completion(text=texts[min(len(seen) - 1, len(texts) - 1)],
                          model="test", provider="ollama")

    monkeypatch.setattr("resume.tailor.complete", fake)
    return seen


def test_a_clean_answer_costs_only_one_call(monkeypatch):
    seen = replies(monkeypatch, answer(CLEAN))
    result = tailor_resume(BASE, "SRE", "Acme", "kubernetes aws")
    assert result.guard.ok
    assert len(seen) == 1, "a passing answer must not trigger a retry"
    assert result.retried is False


def test_a_rejected_answer_is_retried_once_and_kept_when_clean(monkeypatch):
    seen = replies(monkeypatch, answer(INVENTED), answer(CLEAN))
    result = tailor_resume(BASE, "SRE", "Acme", "guardduty pulumi")
    assert len(seen) == 2
    assert result.guard.ok
    assert result.retried is True
    # The near miss is recorded, not hidden: a term that keeps reappearing
    # across postings is a prompt problem, not a retry problem.
    assert any("GuardDuty" in v or "Pulumi" in v for v in result.first_violations)


def test_the_retry_prompt_names_the_exact_claims(monkeypatch):
    """"Do not invent anything" is what the first prompt already said."""
    seen = replies(monkeypatch, answer(INVENTED), answer(CLEAN))
    tailor_resume(BASE, "SRE", "Acme", "guardduty pulumi")
    retry_prompt = seen[1]
    assert "GuardDuty" in retry_prompt
    assert "Pulumi" in retry_prompt
    assert "rejected" in retry_prompt.lower()
    assert "synonym" in retry_prompt.lower(), "or it swaps the word and keeps the claim"


def test_a_retry_that_is_no_better_is_discarded(monkeypatch):
    """Swapping in an equally bad second answer is not progress."""
    seen = replies(monkeypatch, answer(INVENTED), answer(WORSE))
    result = tailor_resume(BASE, "SRE", "Acme", "guardduty pulumi cloudtrail")
    assert len(seen) == 2
    assert not result.guard.ok
    assert result.retried is False, "the first answer should have been kept"
    assert len(result.guard.violations) == 2      # from INVENTED, not the worse three


def test_it_never_retries_twice(monkeypatch):
    """The loop is the dangerous version of this feature."""
    seen = replies(monkeypatch, answer(INVENTED))     # always invents
    result = tailor_resume(BASE, "SRE", "Acme", "guardduty pulumi")
    assert len(seen) == 2, f"expected exactly one retry, got {len(seen) - 1}"
    assert not result.guard.ok


def test_the_retry_can_be_switched_off(monkeypatch):
    class Cfg:
        def get(self, key, default=None):
            return False if key == "resume.retry_on_guard_rejection" else default

    seen = replies(monkeypatch, answer(INVENTED), answer(CLEAN))
    result = tailor_resume(BASE, "SRE", "Acme", "guardduty", cfg=Cfg())
    assert len(seen) == 1
    assert not result.guard.ok
