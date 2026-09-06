from scraper.base import RawJob
from scraper.filters import JobFilter


def make_job(title="Software Engineer", location="Remote - US", description="python, sql"):
    return RawJob(
        source="greenhouse",
        company="Acme",
        company_slug="acme",
        external_id="1",
        title=title,
        location=location,
        description=description,
        application_url="https://example.com/job/1",
    )


def test_title_keyword_match():
    f = JobFilter(title_keywords=["software engineer"])
    assert f.matches(make_job(title="Senior Software Engineer"))
    assert not f.matches(make_job(title="Product Designer"))


def test_title_match_is_case_insensitive():
    f = JobFilter(title_keywords=["backend engineer"])
    assert f.matches(make_job(title="BACKEND ENGINEER, Payments"))


def test_exclude_keywords_win_over_include():
    f = JobFilter(title_keywords=["software engineer"], exclude_title_keywords=["intern"])
    assert not f.matches(make_job(title="Software Engineer Intern"))


def test_empty_title_keywords_accepts_everything():
    f = JobFilter()
    assert f.matches(make_job(title="Anything At All"))


def test_location_filter():
    f = JobFilter(location_keywords=["remote"])
    assert f.matches(make_job(location="Remote - United States"))
    assert not f.matches(make_job(location="Berlin, Germany"))


def test_missing_location_is_kept_for_manual_review():
    f = JobFilter(location_keywords=["remote"])
    assert f.matches(make_job(location=None))


def test_description_required_keywords():
    f = JobFilter(description_required_keywords=["kubernetes"])
    assert not f.matches(make_job(description="python and sql only"))
    assert f.matches(make_job(description="we run Kubernetes in production"))


def test_apply_returns_only_matches():
    f = JobFilter(title_keywords=["engineer"])
    jobs = [make_job(title="Engineer"), make_job(title="Recruiter")]
    assert len(f.apply(jobs)) == 1
