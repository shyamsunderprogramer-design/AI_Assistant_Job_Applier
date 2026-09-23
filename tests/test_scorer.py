"""What the score means, and what it must refuse to reward.

`score_resume` measures how much of a posting's salient vocabulary the resume
covers. On its own that is blind to *which job* the posting is for, and these
tests pin the correction.
"""

import pytest

from resume.scorer import WRONG_ROLE_FLOOR, role_fit, score_resume

# --- role fit -------------------------------------------------------------
# A resume and a job description can share almost all their vocabulary and
# still be different jobs: a data engineer and a DevSecOps engineer both write
# AWS, Python, Kubernetes, Terraform and CI/CD. Measured on live data, that
# scored a DevSecOps posting 93% for a data engineer's resume. These tests
# pin the thing that tells them apart -- the title.

def test_role_fit_exact_title_is_full_credit():
    assert role_fit("Senior Data Engineer", ["data engineer"]) == 1.0


def test_role_fit_held_title_beats_the_search_list():
    # He has actually been one, so it is his job whatever the search says.
    assert role_fit("Curator of Manuscripts", [], ["curator of manuscripts"]) == 1.0


def test_role_fit_scattered_words_earn_partial_credit():
    # "Engineer, Data Platform" is the job; "Data Platform Engineer" is the
    # search. Same role, words out of order -- and the comma must not matter.
    assert role_fit("Engineer, Data Platform", ["data platform engineer"]) == 0.85
    assert role_fit("Engineer - Cloud Infrastructure", ["cloud infrastructure"]) == 1.0
    assert role_fit("Engineer (Data Platform)", ["data platform"]) == 1.0


def test_role_fit_rejects_a_different_field():
    assert role_fit("Senior DevSecOps Engineer", ["data engineer"]) == 0.0


def test_role_fit_does_not_match_a_longer_word():
    # "sre" must not be found inside "Presenter" or "Stressed".
    assert role_fit("Presenter", ["sre"]) == 0.0


def test_role_fit_with_no_titles_is_neutral():
    # No profile means no opinion -- it must not penalise every job.
    assert role_fit("Anything At All", [], []) == 0.0
    assert role_fit("", ["data engineer"]) == 1.0


def test_score_resume_penalises_the_wrong_role():
    resume = "Data engineer. Python, Spark, Airflow, AWS, Kubernetes, Terraform, CI/CD."
    jd = "Python, Spark, Airflow, AWS, Kubernetes, Terraform, CI/CD required."
    right = score_resume(resume, jd, job_title="Data Engineer",
                         search_titles=["data engineer"])
    wrong = score_resume(resume, jd, job_title="DevSecOps Engineer",
                         search_titles=["data engineer"])
    assert right.score > wrong.score
    # Penalised, not erased: the skills really are shared.
    assert wrong.score == pytest.approx(right.score * WRONG_ROLE_FLOOR, abs=1e-3)


# --- postings with no description -----------------------------------------

def test_a_posting_with_no_description_cannot_score_full_marks():
    """A real posting sat at the top of a 2,482-job list: "Embedded Systems
    Engineer", 25 characters of description, scored 1.00 -- above every job
    that had actually been measured. With no text, coverage is undefined, and
    reporting 100% claims certainty from no evidence."""
    from resume.scorer import TITLE_ONLY_CEILING, score_basis

    resume = "Systems engineer. Kubernetes, AWS, Terraform, Python."
    thin = score_resume(resume, "Embedded Systems Engineer",
                        job_title="Embedded Systems Engineer",
                        search_titles=["systems engineer"])
    assert score_basis("Embedded Systems Engineer") == "title"
    assert thin.score <= TITLE_ONLY_CEILING


def test_a_real_description_is_not_capped():
    resume = "DevOps engineer. Kubernetes, AWS, Terraform, Python, CI/CD."
    jd = ("We need a DevOps engineer with Kubernetes, AWS, Terraform and "
          "Python for our CI/CD platform. ") * 12
    from resume.scorer import TITLE_ONLY_CEILING

    full = score_resume(resume, jd, job_title="DevOps Engineer",
                        search_titles=["devops"])
    assert full.score > TITLE_ONLY_CEILING


def test_the_cap_sits_below_the_default_threshold():
    """So an unmeasurable posting reads as "worth a look", not "best match"."""
    from resume.scorer import TITLE_ONLY_CEILING

    assert TITLE_ONLY_CEILING < 0.60
