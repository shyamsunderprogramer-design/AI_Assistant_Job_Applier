"""A posting pasted with its whole page keeps only the posting (backend/core/jobfields.posting_only)."""

from backend.core.jobfields import posting_only

TITLE = "DevOps Engineer - US East Coast/ Central"
BODY = ("Upwind is a next-generation cloud security platform. You will build and run our Kubernetes "
        "infrastructure on AWS with Terraform and Helm, own CI/CD in GitHub Actions and ArgoCD, keep "
        "Prometheus and Grafana alerting useful, and automate everything you can in Python, Bash or Go. "
        "Experience with RabbitMQ, Redis and Postgres in production is a plus, as is running a "
        "high-scale distributed system on Linux with a strong sense of ownership.")
PAGE = ("Adzuna logo\nLogin\nRegister\nEmployers\nWhat?\njob, company, title\nWhere?\nSearch\nAdvanced\n"
        f"{TITLE}\n\nUpwind Security\nSan Francisco, California, 94105\n{BODY}\nApply for this job\n"
        "Stats for this job\nSalary comparison:\nIT Jobs\naverage $136,741\nReceive similar jobs by email\n"
        "Similar jobs\nSolutions Engineer - US, West Coast\nNeural Concept San Francisco, California\n"
        "Popular searches\nDevops engineer\n© 2026 ADZUNA LTD")


def test_a_page_pasted_whole_keeps_only_the_posting():
    # 22 Oct 2026: the site's menus, salary charts and "Similar jobs" came back as work to claim.
    kept = posting_only(PAGE, TITLE)
    assert kept.startswith(TITLE) and BODY in kept
    for chrome in ("Adzuna logo", "Stats for this job", "Neural Concept", "West Coast", "Popular searches",
                   "Apply for this job"):
        assert chrome not in kept, chrome


def test_a_posting_with_no_page_around_it_is_left_as_it_is():
    assert posting_only(BODY + "\n\nSimilar jobs is not a phrase this posting uses.", TITLE).startswith(BODY)
    assert posting_only(BODY, "") == BODY


def test_a_cut_that_would_leave_almost_nothing_is_not_made():
    short = f"{TITLE}\nShort teaser only.\nSimilar jobs\nAnother job at another place, with words."
    assert posting_only(short, TITLE) == short


def test_hands_on_is_not_a_kind_of_work_to_claim():
    from ml.resume.ats import compare
    jd = BODY + "\nProven hands-on experience with Helm. Hands-on with multiple cloud vendors."
    assert "hands-on" not in compare("Ran Kubernetes on AWS with Terraform.", None, jd, company="Upwind")["concepts"]
