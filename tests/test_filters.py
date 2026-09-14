import pytest

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


# -- robots.txt status handling (README.md §C5) ----------------------------

class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _client_seeing(status, text=""):
    """A PoliteClient whose robots.txt fetch returns `status`."""
    from scraper.http_client import HttpSettings, PoliteClient

    client = PoliteClient(HttpSettings(user_agent="test-agent"))
    client._session.get = lambda url, **kw: _FakeResponse(status, text)
    return client


def test_401_on_robots_means_unavailable_not_forbidden():
    """RFC 9309 §2.3.1.3: a 4xx means robots.txt is unavailable and the crawler
    MAY proceed. Treating 401 as a blanket Disallow made api.ashbyhq.com — a
    documented PUBLIC job-board API — unscrapeable."""
    assert _client_seeing(401).allowed("https://api.example.com/jobs") is True


def test_403_on_robots_also_means_unavailable():
    assert _client_seeing(403).allowed("https://api.example.com/jobs") is True


def test_404_on_robots_allows():
    assert _client_seeing(404).allowed("https://api.example.com/jobs") is True


def test_strict_mode_restores_the_cautious_reading():
    client = _client_seeing(401)
    client.settings.strict_robots_on_4xx = True
    assert client.allowed("https://api.example.com/jobs") is False


def test_5xx_on_robots_disallows():
    """A server having a bad day is not permission (RFC 9309 §2.3.1.4)."""
    assert _client_seeing(503).allowed("https://api.example.com/jobs") is False


def test_a_real_disallow_directive_is_still_honoured():
    robots = "User-agent: *\nDisallow: /private/"
    client = _client_seeing(200, robots)
    assert client.allowed("https://example.com/private/x") is False
    assert client.allowed("https://example.com/public/x") is True


# -- the search must never silently become somebody else's -------------------

class _Cfg:
    """Minimal config stand-in: dotted lookups with defaults."""

    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def _write_profile(path, titles):
    body = "seniority: senior\nyears_experience: 9\nderived_from: none\n"
    if titles:
        body += "title_keywords:\n" + "".join(f'  - "{t}"\n' for t in titles)
    else:
        body += "title_keywords: []\n"
    path.write_text(body, encoding="utf-8")


def test_a_resume_that_derives_nothing_stops_the_run(tmp_path, monkeypatch):
    """The worst bug this tool had, pinned.

    An electrical engineer uploads his resume. Nothing in it is recognised, so
    the derived profile comes back with no titles — and the search silently
    became the software keywords sitting in config.yaml. He would have got a
    tidy list of Python jobs with nothing anywhere to tell him the search was
    never his. A run that stops and says why is worth far more than one that
    succeeds at the wrong thing.
    """
    import config.loader as loader_mod
    from scraper.filters import UndeterminedSearch, resolve_filter

    profile_path = tmp_path / "search_profile.yaml"
    _write_profile(profile_path, [])
    monkeypatch.setattr(loader_mod, "PROJECT_ROOT", tmp_path)

    cfg = _Cfg(**{
        "filters.profile_path": "search_profile.yaml",
        "filters.title_keywords": ["software engineer", "python"],
    })

    with pytest.raises(UndeterminedSearch) as raised:
        resolve_filter(cfg)

    # The message has to be actionable, not just a refusal: it names the file
    # to edit and the command to re-run.
    message = str(raised.value)
    assert "search_profile.yaml" in message
    assert "main.py profile" in message
    # And it must never quietly hand over the config's software keywords.
    assert "software engineer" not in message


def test_no_resume_at_all_still_uses_the_config(tmp_path, monkeypatch):
    """The config fallback is not removed — it is narrowed to the case it was
    written for: nobody has uploaded a resume yet."""
    import config.loader as loader_mod
    from scraper.filters import resolve_filter

    monkeypatch.setattr(loader_mod, "PROJECT_ROOT", tmp_path)
    cfg = _Cfg(**{
        "filters.profile_path": "does_not_exist.yaml",
        "filters.title_keywords": ["software engineer"],
    })

    assert resolve_filter(cfg).title_keywords == ["software engineer"]


def test_a_derived_search_wins_over_the_config(tmp_path, monkeypatch):
    import config.loader as loader_mod
    from scraper.filters import resolve_filter

    profile_path = tmp_path / "search_profile.yaml"
    _write_profile(profile_path, ["registered nurse", "staff nurse"])
    monkeypatch.setattr(loader_mod, "PROJECT_ROOT", tmp_path)

    cfg = _Cfg(**{
        "filters.profile_path": "search_profile.yaml",
        "filters.title_keywords": ["software engineer"],
    })

    assert resolve_filter(cfg).title_keywords == ["registered nurse", "staff nurse"]
