"""The recruiter review and skim test: shape, honesty filters, and the XYZ ask list."""

import json
from types import SimpleNamespace

from ml.tailoring import review


def fake(answer):
    return lambda system, user, cfg, max_tokens=0: SimpleNamespace(text=json.dumps(answer))


def test_review_scores_from_its_breakdown_and_drops_keywords_the_resume_has(monkeypatch):
    monkeypatch.setattr("ml.resume.llm.complete", fake({
        "score": 99, "breakdown": {"skills": 20, "experience_level": 18, "keywords": 12, "impact": 15},
        "missing_keywords": ["Terraform", "Kafka", "SOC 2", "Go", "Datadog", "Snowflake"],
        "red_flags": [{"flag": "Summary is generic", "fix": "Lead with years and platforms"}, "No Kafka", "x", "y"]}))
    r = review.recruiter_review("Built Terraform modules. Ran Datadog.", "JD text", "Acme", cfg=None)
    assert r["score"] == 65                                   # the breakdown's sum, not the model's 99
    assert r["missing_keywords"] == ["Kafka", "SOC 2", "Go", "Snowflake"]
    assert len(r["red_flags"]) == 3 and r["red_flags"][0]["fix"] == "Lead with years and platforms"


def test_skim_rewrites_flag_numbers_the_resume_never_states(monkeypatch):
    monkeypatch.setattr("ml.resume.llm.complete", fake({
        "skipped": [{"section": "Summary", "why": "generic", "rewrite": "Cut MTTR 38% and saved 2M dollars."}],
        "ats_problems": ["Two-column skills table"], "first_six_seconds": "A DevOps engineer."}))
    s = review.skim_test("Cut MTTR 38% with Prometheus.", "JD", cfg=None)
    assert s["skipped"][0]["unsupported_numbers"] == ["2"]
    assert s["ats_problems"] == ["Two-column skills table"]


def test_guidance_only_allows_honest_keywords():
    g = review.guidance({"missing_keywords": ["Kafka"], "red_flags": [{"flag": "No metrics", "fix": ""}]})
    assert "ONLY where the resume already shows" in g and "Kafka" in g and "No metrics" in g
    assert review.guidance({}) == ""


def test_lines_without_a_number_are_asked_for():
    assert review.needs_numbers(["Cut MTTR 38%.", "•\u2002Built pipelines in Jenkins.", "", "Infrastructure as Code & Cloud"]) == ["Built pipelines in Jenkins."]
