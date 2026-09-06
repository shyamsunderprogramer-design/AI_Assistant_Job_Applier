"""Pipeline tests — in-memory DB, no network, no API key."""

import pytest

from db import session as session_mod
from db.models import Job
from db.session import get_session, init_engine
from resume.pipeline import score_jobs

STRONG_JD = "Requirements:\n- Strong Python and Django\n- PostgreSQL and SQL\n- Airflow pipelines"
WEAK_JD = "Requirements:\n- Expert React, Figma, and CSS animation for frontend design systems"

BASE_RESUME = """JANE DOE
SKILLS
Python, Django, PostgreSQL, SQL, Airflow
EXPERIENCE
- Built Python and Django services backed by PostgreSQL
- Ran Airflow data pipelines with SQL
"""


class FakeConfig:
    """Minimal stand-in for config.loader.Config."""

    def __init__(self, resume_path, min_score=0.25):
        self._values = {
            "resume.base_path": str(resume_path),
            "resume.min_score": min_score,
            "resume.output_dir": "resume/output",
        }

    def get(self, path, default=None):
        return self._values.get(path, default)


@pytest.fixture
def env(tmp_path):
    init_engine("sqlite:///:memory:")
    resume_path = tmp_path / "base_resume.txt"
    resume_path.write_text(BASE_RESUME, encoding="utf-8")
    yield resume_path
    session_mod._engine = None
    session_mod._SessionFactory = None


def add_job(jd, title="Backend Engineer", ext="1"):
    with get_session() as s:
        s.add(Job(
            source="greenhouse", company="Acme", company_slug="acme", external_id=ext,
            title=title, location="Remote", description=jd, requirements=jd,
            application_url="https://example.com/1", content_hash=ext,
        ))


def status_of(ext="1"):
    with get_session() as s:
        return s.query(Job).filter_by(external_id=ext).one().status


def test_scoring_ranks_relevant_job_higher(env):
    add_job(STRONG_JD, ext="1")
    add_job(WEAK_JD, title="Frontend Engineer", ext="2")
    outcomes = {o.job_id: o.score for o in score_jobs(FakeConfig(env))}
    scores = sorted(outcomes.values(), reverse=True)
    assert scores[0] > scores[1]


def test_low_score_is_flagged_for_manual_review(env):
    add_job(WEAK_JD)
    score_jobs(FakeConfig(env))
    assert status_of() == "Manual Review"


def test_flag_clears_when_the_job_later_clears_the_threshold(env):
    add_job(STRONG_JD)
    score_jobs(FakeConfig(env, min_score=0.99))   # impossible bar -> flagged
    assert status_of() == "Manual Review"

    score_jobs(FakeConfig(env, min_score=0.01), rescore=True)  # now it passes
    assert status_of() == "Not Applied"


def test_user_set_status_is_never_touched_by_scoring(env):
    add_job(WEAK_JD)
    with get_session() as s:
        s.query(Job).one().status = "Applied"

    score_jobs(FakeConfig(env), rescore=True)
    assert status_of() == "Applied"


def test_scores_are_persisted_and_marked_for_reexport(env):
    add_job(STRONG_JD)
    score_jobs(FakeConfig(env))
    with get_session() as s:
        job = s.query(Job).one()
        assert job.ats_match_score is not None
        assert job.exported_to_excel is False


def test_missing_base_resume_raises_clearly(tmp_path, env):
    add_job(STRONG_JD)
    with pytest.raises(FileNotFoundError):
        score_jobs(FakeConfig(tmp_path / "nonexistent" / "resume.docx"))
