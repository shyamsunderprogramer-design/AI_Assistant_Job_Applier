"""Removing a resume must remove the search derived from it, and every
candidate that could take its place.

Both halves matter and both were real bugs waiting to happen on this machine,
which held two resumes at once:

  * a profile left behind without its resume would search for the previous
    person's job titles until somebody noticed;
  * deleting only the *active* file promotes the next-newest, so "remove"
    would quietly mean "switch back to the last user".
"""

import pytest

import webapp.app as webapp


@pytest.fixture
def site(tmp_path, monkeypatch):
    (tmp_path / "resume").mkdir()
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(webapp, "resume_dir", lambda: tmp_path / "resume")
    monkeypatch.setattr("config.loader.PROJECT_ROOT", tmp_path)
    webapp.app.config["TESTING"] = True
    return tmp_path


def test_remove_takes_every_resume_and_the_profile(site):
    (site / "resume/base_resume.pdf").write_text("the current one")
    (site / "resume/base_resume.docx").write_text("the previous person's")
    (site / "config/search_profile.yaml").write_text("families:\n  - finance\n")

    with webapp.app.test_client() as client:
        body = client.post("/resume/remove").get_json()

    assert body["ok"] is True
    assert body["removed"] == ["base_resume.docx", "base_resume.pdf",
                               "search_profile.yaml"]
    # Nothing left that find_base_resume could promote.
    assert list((site / "resume").iterdir()) == []
    assert not (site / "config/search_profile.yaml").exists()


def test_remove_says_so_when_there_is_nothing_to_remove(site):
    with webapp.app.test_client() as client:
        response = client.post("/resume/remove")
    assert response.status_code == 404
    assert response.get_json()["ok"] is False


def test_remove_keeps_the_jobs_already_found(site, monkeypatch):
    """Stored postings are evidence, and cost a scrape to replace."""
    (site / "resume/base_resume.pdf").write_text("x")
    seen = []
    monkeypatch.setattr(webapp, "run_command",
                        lambda *a, **k: seen.append(a) or (True, ""))
    with webapp.app.test_client() as client:
        assert client.post("/resume/remove").get_json()["ok"] is True
    assert seen == []          # no prune, no rescore, no scrape
