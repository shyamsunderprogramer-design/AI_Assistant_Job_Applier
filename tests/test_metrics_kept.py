"""A rewrite must never cost a number.

Measured across every tailoring this project had produced: 111 figures in the
original bullets, 68 of them deleted by the rewrite -- 61%. "99.98% uptime",
"42% faster", "60% reduction", "200+ workloads". Those are the strongest
lines on a resume and the most common reason a recruiter picks up the phone,
and they were being thrown away because the quickest way to shorten a bullet
is to cut its result clause.

The prompt now asks. This enforces, because a prompt is a request.
"""

import pytest

from resume.tailor import _keep_metrics, _metrics


@pytest.mark.parametrize("text,expected", [
    ("Cut MTTR by 38%.", {"38%"}),
    ("Held 99.95% uptime.", {"99.95%"}),
    ("Provisioned 200+ workloads.", {"200"}),
    ("Saved $1.2M annually.", {"$1.2"}),
    ("Improved throughput 3x.", {"3x"}),
    ("Led a team and shipped features.", set()),
    # A year is not an achievement metric, and a two-digit count is noise.
    ("Worked there from 2019.", {"2019"}),
])
def test_metrics_are_recognised(text, expected):
    assert _metrics(text) == expected


def test_a_rewrite_that_drops_a_number_is_rejected():
    bullets = [{"original": "Built observability, achieving 38% faster MTTR.",
                "tailored": "Built observability, achieving faster MTTR.",
                "reason": "uses the JD's words"}]
    assert _keep_metrics(bullets) == 1
    assert bullets[0]["tailored"] == "Built observability, achieving 38% faster MTTR."
    assert "38%" in bullets[0]["reason"], "the note must say why it was kept"


def test_a_rewrite_that_keeps_the_number_is_left_alone():
    tailored = "Built observability with Datadog, achieving 38% faster MTTR."
    bullets = [{"original": "Built observability, achieving 38% faster MTTR.",
                "tailored": tailored, "reason": "named the JD's tool"}]
    assert _keep_metrics(bullets) == 0
    assert bullets[0]["tailored"] == tailored


def test_every_number_must_survive_not_just_one():
    bullets = [{"original": "Provisioned 200 workloads, cutting setup 60%.",
                "tailored": "Provisioned 200 workloads faster.",
                "reason": "shorter"}]
    assert _keep_metrics(bullets) == 1, "dropping 60% must be caught"


def test_a_bullet_with_no_numbers_is_untouched():
    bullets = [{"original": "Led the migration off the legacy platform.",
                "tailored": "Led the cloud migration off the legacy platform.",
                "reason": "JD vocabulary"}]
    assert _keep_metrics(bullets) == 0
    assert bullets[0]["tailored"].startswith("Led the cloud")


def test_the_prompt_says_it_too():
    """Belt and braces: the model should not be producing them in the first
    place, because a restored bullet has lost its tailoring."""
    from resume.tailor import SYSTEM_PROMPT

    assert "NEVER DELETE A NUMBER" in SYSTEM_PROMPT
    assert "99.95%" in SYSTEM_PROMPT or "38%" in SYSTEM_PROMPT
