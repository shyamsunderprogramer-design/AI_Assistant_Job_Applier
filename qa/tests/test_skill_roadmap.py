"""The skill roadmap: gaps in order, bridges only in the person's own words."""

from ml.tailoring import roadmap

MATCHES = [
    {"status": "found", "requirement": {"original": "Terraform", "normalized": "terraform", "priority": "required"}},
    {"status": "unclear", "requirement": {"original": "Oracle Kubernetes Engine (OKE)", "normalized": "oke",
                                          "priority": "required", "context": "Manage AKS and OKE"},
     "evidence": [{"text": "Administered EKS, AKS, and OpenShift clusters."}]},
    {"status": "not_found", "requirement": {"original": "Dynatrace", "normalized": "dynatrace", "priority": "preferred"}},
    {"status": "not_found", "requirement": {"original": "SAS tokens", "normalized": "sas tokens", "priority": "required"}},
]
RESUME = "Administered EKS, AKS, and OpenShift clusters.\nBuilt Grafana dashboards for SLOs.\n"


def test_gaps_put_required_first_and_carry_the_closest_lines():
    gaps = roadmap.gaps_of(MATCHES, ["Dynatrace", "landing zones"])
    assert [g["skill"] for g in gaps] == ["SAS tokens", "Oracle Kubernetes Engine (OKE)", "Dynatrace", "landing zones"]
    assert gaps[1]["evidence"] == ["Administered EKS, AKS, and OpenShift clusters."]
    assert gaps[3]["priority"] == "keyword"


def test_a_bridge_that_is_not_on_the_resume_is_dropped():
    reply = {"skills": [
        {"skill": "Oracle Kubernetes Engine (OKE)", "what_it_is": "Oracle's managed Kubernetes.",
         "bridge": {"you_have": "Administered EKS, AKS, and OpenShift clusters", "how_it_transfers": "Same API."},
         "plan": [{"step": "Free tier cluster", "resource": "Oracle Cloud free tier", "hours": 4},
                  {"step": "Deploy an app", "resource": "OKE docs", "hours": "6"}],
         "project": "Run one app on OKE.", "interview_answer": "I run EKS and AKS; I am learning OKE."},
        {"skill": "Dynatrace", "bridge": {"you_have": "Ran Dynatrace for five years"},
         "plan": [{"step": "Free trial", "hours": 3}]}]}
    out = roadmap.build(RESUME, "jd", roadmap.gaps_of(MATCHES), cfg=None, ask=lambda *a: reply)
    oke, dyna = out
    assert oke["you_have"] == "Administered EKS, AKS, and OpenShift clusters"
    assert oke["hours"] == 10 and oke["priority"] == "required" and oke["status"] == "unclear"
    assert dyna["you_have"] == ""                       # invented experience never shown as theirs


def test_nothing_to_plan_is_no_call():
    assert roadmap.build(RESUME, "jd", [], cfg=None, ask=lambda *a: 1 / 0) == []


def test_an_answer_claiming_progress_is_flagged():
    assert roadmap.claims_progress("I'm now applying that to SAS and I've been working through the docs.")
    assert roadmap.claims_progress("I am currently learning OKE.")
    assert not roadmap.claims_progress("I run EKS and AKS; to ramp up on OKE I would start with the free tier.")
