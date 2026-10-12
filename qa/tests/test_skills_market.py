"""Skills across the best jobs: what to learn next, what to lead with."""

from ml.tailoring import keywords, market


def test_skills_are_counted_across_jobs_and_split_by_the_resume(monkeypatch):
    asks = {1: {"istio": 2.0, "terraform": 3.0, "support": 1.0},
            2: {"istio": 2.0, "terraform": 3.0},
            3: {"istio": 1.0, "google cloud": 1.0, "support": 1.0},
            4: {"terraform": 1.0, "google cloud": 1.0}}
    monkeypatch.setattr(keywords, "weights", lambda jd, company=None: asks[int(jd.split()[0])])
    jobs = [{"id": i, "title": str(i), "company": "Acme", "score": 90,
             "description": "Run Istio and Terraform on Google Cloud, with support."} for i in asks]
    out = market.scan("Built Terraform modules on GCP.", jobs)
    missing = {e["term"]: e for e in out["missing"]}
    assert set(missing) == {"istio"}                 # "support" is no skill; Google Cloud is their GCP
    assert missing["istio"]["jobs"] == 3 and missing["istio"]["share"] == 75
    assert missing["istio"]["name"] == "Istio"       # as the postings write it
    assert missing["istio"]["contexts"] == ["Run Istio and Terraform on Google Cloud, with support."]
    assert [e["term"] for e in out["strengths"]][:1] == ["terraform"]


def test_a_full_name_is_taken_from_the_posting_but_not_a_sentence():
    assert market._full_name("active", ["Administer Active Directory and Entra ID."]) == "Active Directory"
    assert market._full_name("saas", ["A SaaS platform that powers the world."]) == "saas"
