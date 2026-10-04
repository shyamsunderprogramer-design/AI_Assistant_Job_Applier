"""Requirements, matching, score and version checks for Resume Tailoring."""

import json

import pytest

from ml.tailoring import matching, requirements, versions
from ml.tailoring.matching import match
from ml.tailoring.reader import Block, Extracted
from ml.tailoring.requirements import Requirement, extract
from ml.tailoring.structure import build


def resume(lines):
    """A resume model from (text, list_item) lines."""
    blocks = [Block(t, f"line {i}", list_item=li) for i, (t, li) in enumerate(lines, 1)]
    return build(Extracted("text", blocks))


RESUME = resume([
    ("Jordan Example", False), ("jordan@example.com", False),
    ("EXPERIENCE", False),
    ("Operations Analyst, Acme Corp     Jan 2019 – Dec 2021", False),
    ("Ran K8s clusters on EKS for 40 services.", True),
    ("Built pipelines in GitLab CI for every release.", True),
    ("Wrote Terraform 1.5 modules for AWS accounts.", True),
    ("SKILLS", False),
    ("Datadog, Python", False),
    ("EDUCATION", False),
    ("B.S. Computer Science, State University     2013 – 2017", False),
])


def req(rid, original, normalized, category="tool", priority="required"):
    return Requirement(rid, original, normalized, category, priority, original)


def status(report, rid):
    return next(m for m in report.matches if m.requirement["id"] == rid)


def test_exact_and_synonym_matches_are_direct():
    r = match([req("r1", "Kubernetes", "kubernetes"), req("r2", "Terraform", "terraform")], RESUME, use_model=False)
    k8s = status(r, "r1")
    assert k8s.status == "direct" and "K8s" in k8s.evidence[0]["text"]
    assert status(r, "r2").status == "direct"


def test_a_similar_technology_is_related_never_direct():
    r = match([req("r1", "Jenkins", "jenkins")], RESUME, use_model=False)
    m = status(r, "r1")
    assert m.status == "related" and "GitLab CI" in m.evidence[0]["text"]
    assert "not hands-on Jenkins" in m.explanation


def test_a_tool_only_in_the_skills_list_is_unclear():
    m = status(match([req("r1", "Datadog", "datadog")], RESUME, use_model=False), "r1")
    assert m.status == "unclear" and m.value == 0


def test_a_requirement_with_no_support_is_a_gap_and_nothing_is_added():
    r = match([req("r1", "Rust", "rust")], RESUME, use_model=False)
    assert status(r, "r1").status == "not_found" and r.gaps[0].requirement["original"] == "Rust"
    assert not any("Rust" in f.text for f in RESUME.facts)


def test_a_repeated_requirement_is_counted_once():
    jd = "Requirements:\n- Kubernetes experience.\n- Hands-on K8s in production.\n- Terraform."
    reqs, _ = extract(jd, use_model=False)
    assert [r.normalized for r in reqs].count("kubernetes") == 1
    k = next(r for r in reqs if r.normalized == "kubernetes")
    assert k.also == ["K8s"]


def test_priority_comes_from_the_postings_words():
    jd = ("Requirements:\n- Experience with Terraform modules.\n- Go experience is a plus.\n"
          "Nice to have:\n- Experience with Datadog dashboards.")
    reqs = {r.normalized: r.priority for r in extract(jd, use_model=False)[0]}
    assert reqs["terraform"] == "required"
    assert reqs["datadog"] == "preferred"                 # under a "Nice to have" heading
    assert next(v for k, v in reqs.items() if k in ("go", "golang")) == "preferred"   # "is a plus"


def test_the_score_follows_the_formula():
    reqs = [req("r1", "Kubernetes", "kubernetes"),                       # required, direct   3 × 1.0
            req("r2", "Jenkins", "jenkins"),                             # required, related  3 × 0.5
            req("r3", "Rust", "rust", priority="preferred")]             # preferred, missing 1 × 0
    r = match(reqs, RESUME, use_model=False)
    assert r.total_weight == 7 and r.score == round((3 + 1.5) / 7 * 100, 1)
    assert [m.weight for m in r.matches] == [3, 3, 1]
    assert sum(m.contribution for m in r.matches) == pytest.approx(r.score, abs=0.2)


def test_a_different_title_with_relevant_duties_still_matches(monkeypatch):
    """The title is "Operations Analyst"; the posting wants a platform engineer's duties."""
    def fake(system, user, cfg=None, **kw):
        assert "Judge by what the person DID" in system
        return type("C", (), {"text": json.dumps({
            "matches": [{"id": "r1", "status": "direct", "evidence": ["f4"],
                         "explanation": "Running EKS clusters for 40 services is operating Kubernetes infrastructure."}],
            "alignment": "The work shows platform operation despite a different title."})})()
    monkeypatch.setattr("ml.resume.llm.complete", fake)
    r = match([req("r1", "Operate production container infrastructure", "operate container infrastructure",
                   category="responsibility")], RESUME, use_model=True)
    m = status(r, "r1")
    assert m.status == "direct" and m.method == "model"
    assert RESUME.entries[0].title == "Operations Analyst"            # the title is not touched
    assert "despite a different title" in r.alignment


def test_a_model_claim_with_no_real_evidence_is_lowered(monkeypatch):
    def fake(system, user, cfg=None, **kw):
        return type("C", (), {"text": json.dumps({"matches": [
            {"id": "r1", "status": "direct", "evidence": ["f99"], "explanation": "Has it."},
            {"id": "r2", "status": "direct", "evidence": ["f5"], "explanation": "Has it."}]})})()
    monkeypatch.setattr("ml.resume.llm.complete", fake)
    r = match([req("r1", "Lead a team of 10", "lead team", category="responsibility"),
               req("r2", "Rust", "rust")], RESUME, use_model=True)
    assert status(r, "r1").status == "not_found"                      # cited a line that does not exist
    assert status(r, "r2").status == "unclear"                        # cited a line that does not name it


def test_years_are_added_up_from_the_job_dates():
    m = status(match([req("r1", "3+ years of Kubernetes", "3+ years kubernetes", category="experience")],
                     RESUME, use_model=False), "r1")
    assert m.status == "direct" and "3.0 years" in m.explanation


def test_a_version_released_after_the_work_ended_is_flagged():
    model = resume([("EXPERIENCE", False), ("Engineer, Acme     Jan 2019 – Dec 2020", False),
                    ("Ran Kubernetes 1.27 clusters.", True), ("Used Terraform 0.11 for modules.", True)])
    cycles = {"kubernetes": ([{"cycle": "1.27", "releaseDate": "2023-04-11"}], "test data")}
    checks = versions.check(model, fetch=lambda product: cycles.get(product))
    k = next(c for c in checks if c.tool.lower() == "kubernetes")
    assert k.status == "mismatch" and "after this work ended" in k.detail and k.source == "test data"
    assert next(c for c in checks if c.tool.lower() == "terraform").status == "unknown"


def test_a_major_version_without_its_first_release_is_unknown_not_guessed():
    assert versions.release_of([{"cycle": "2.9", "releaseDate": "2019-10-31"}], "2.x") is None
    assert versions.release_of([{"cycle": "2.0", "releaseDate": "2016-01-12"}], "2.x") == "2016-01-12"
    assert versions.release_of([{"cycle": "17", "releaseDate": "2021-09-14"}], "17") == "2021-09-14"


def test_a_status_that_contradicts_its_explanation_is_asked_again(monkeypatch):
    calls = []

    def fake(system, user, cfg=None, **kw):
        calls.append(user)
        if len(calls) == 1:
            return type("C", (), {"text": json.dumps({"matches": [
                {"id": "r1", "status": "unclear", "evidence": ["f4"],
                 "explanation": "The resume explicitly shows running EKS clusters."}]})})()
        return type("C", (), {"text": json.dumps({"matches": [
            {"id": "r1", "status": "direct", "evidence": ["f4"], "explanation": "Runs EKS clusters for 40 services."}]})})()
    monkeypatch.setattr("ml.resume.llm.complete", fake)
    r = match([req("r1", "Operate managed clusters", "operate clusters", category="responsibility")],
              RESUME, use_model=True)
    m = status(r, "r1")
    assert len(calls) == 2 and "contradicts" in calls[1]
    assert m.status == "direct" and "Re-checked" in m.explanation


def test_a_degree_in_a_biography_is_not_a_requirement():
    from ml.tailoring.requirements import _by_rules
    bio = "Before Acme, our founder was a professor and did a math PhD at a university."
    assert not [i for i in _by_rules(bio) if i["category"] == "education"]
    assert [i for i in _by_rules("- Bachelor's degree in Computer Science or equivalent.") if i["category"] == "education"]
