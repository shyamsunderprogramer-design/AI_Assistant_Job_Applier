"""Cutting a master resume down to one posting — no PDF rendering needed.

`fit_to_pages` takes its page counter as a parameter; these tests count
"pages" as every 60 words of the planned document, so what is tested is the
selection, not reportlab.
"""

from ml.resume.fit import fit_to_pages
from ml.resume.parser import parse_text
from ml.resume.tailor import TailorResult
from ml.resume.writer import plan_document

MASTER = """JANE DOE
jane@example.com | 555-0100

SUMMARY
Platform engineer with 10 years across cloud and data.

EXPERIENCE
SENIOR PLATFORM ENGINEER, ACME, USA     Jan 2022 – Present
- Built Terraform modules for 200+ AWS workloads, cutting setup time by 60%.
- Ran Kubernetes clusters on EKS with ArgoCD GitOps and Helm releases.
- Organised the office book club and the quarterly team offsite events.
- Designed Prometheus and Grafana alerting that reduced MTTR by 35%.
- Wrote the onboarding wiki for new starters in the finance department.
Infrastructure as Code (IaC) & Provisioning
- Automated Terraform drift detection across 40 accounts with Sentinel policies.
- Coordinated catering vendors for the annual company summer gathering.
DATA ENGINEER, GLOBEX, USA     Jan 2016 – Dec 2021
- Built Spark pipelines over 5 TB of daily events into a Delta lakehouse.
- Tuned Oracle stored procedures for the legacy billing reports module.
- Migrated batch jobs to Airflow, cutting failed runs by 70%.
- Maintained the printer fleet and desk phone inventory for two floors.

EDUCATION
BS Computer Science, State University     2012 – 2016
"""

JD = """Senior Platform Engineer. You will own Terraform infrastructure on AWS,
run Kubernetes (EKS) with ArgoCD and Helm, and build Prometheus and Grafana
observability to cut MTTR. Terraform, Kubernetes, AWS, Prometheus required."""


def words_in(base, result) -> int:
    return sum(len(text.split()) for _, text in plan_document(base, result))


def pages_of(per_page):
    return lambda base, result: -(-words_in(base, result) // per_page)


def text_of(base, result) -> str:
    return "\n".join(text for _, text in plan_document(base, result))


def fit(max_pages=2, per_page=60, result=None):
    base = parse_text(MASTER)
    result = result or TailorResult(summary=None)
    pages = fit_to_pages(base, result, JD, max_pages=max_pages, pages=pages_of(per_page))
    return base, result, pages


def test_it_fits_the_page_limit():
    base, result, pages = fit()
    assert pages <= 2
    assert words_in(base, result) <= 120


def test_what_the_posting_asks_for_is_kept_and_the_rest_goes():
    base, result, _ = fit()
    text = text_of(base, result)
    assert "Terraform modules for 200+ AWS workloads" in text
    assert "Kubernetes clusters on EKS" in text
    assert "book club" not in text and "catering" not in text and "printer fleet" not in text


def test_every_role_and_date_survives():
    base, result, _ = fit(max_pages=1)
    text = text_of(base, result)
    assert "SENIOR PLATFORM ENGINEER, ACME, USA" in text
    assert "DATA ENGINEER, GLOBEX, USA" in text
    assert "BS Computer Science, State University" in text


def test_each_role_keeps_at_least_two_bullets_even_when_irrelevant():
    """An old role the posting barely needs still shows two lines of work."""
    base, result, _ = fit(max_pages=1, per_page=10)
    kept = [t for k, t in plan_document(base, result) if k == "bullet"]
    globex = ["Spark pipelines", "Oracle stored", "Airflow", "printer fleet"]
    assert sum(any(g in b for g in globex) for b in kept) >= 2


def test_a_group_title_goes_with_its_last_line():
    base, result, _ = fit()
    text = text_of(base, result)
    if "Automated Terraform drift" not in text:
        assert "Infrastructure as Code (IaC)" not in text


def test_every_cut_is_recorded_with_a_reason():
    base, result, _ = fit()
    assert result.omitted and all(e["reason"] for e in result.omitted)
    assert result.fitted


def test_nothing_is_added_or_reworded():
    base, result, _ = fit()
    master_lines = {l.strip().lstrip("- ") for l in MASTER.splitlines()}
    for kind, text in plan_document(base, result):
        if kind == "bullet":
            assert text in master_lines


def test_the_models_own_cuts_are_honoured():
    asked = TailorResult(summary=None, omitted=[
        {"original": "- Designed Prometheus and Grafana alerting that reduced MTTR by 35%.",
         "reason": "model chose to drop it"}])
    base, result, _ = fit(max_pages=5, result=asked)
    assert "Prometheus and Grafana alerting" not in text_of(base, result)


def test_a_resume_that_already_fits_is_left_whole():
    base, result, pages = fit(max_pages=50)
    assert "book club" in text_of(base, result)
    assert pages <= 50


def test_zero_pages_means_keep_everything():
    base, result, _ = fit(max_pages=0)
    assert result.omitted == []
