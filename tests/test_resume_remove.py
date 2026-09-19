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


# --- the name the user actually chose -------------------------------------
# Every upload is stored as base_resume.<ext>, which is what makes the active
# resume unambiguous. The cost is that the page then shows everybody the same
# meaningless "base_resume.pdf" instead of the file they recognise.

def test_upload_remembers_the_name_the_user_chose(site, monkeypatch):
    import io

    monkeypatch.setattr(webapp, "run_command", lambda *a, **k: (True, ""))
    monkeypatch.setattr(webapp, "derived_search", lambda: {"ok": True})

    with webapp.app.test_client() as client:
        body = client.post(
            "/upload",
            data={"resume": (io.BytesIO(b"resume text"), "Balarama_SupplyChain.pdf")},
            content_type="multipart/form-data",
        ).get_json()

    assert body["file"] == "Balarama_SupplyChain.pdf"   # what they chose
    assert body["stored"] == "base_resume.pdf"          # what it is called on disk

    label = webapp.resume_label()
    assert label["name"] == "Balarama_SupplyChain.pdf"
    assert label["stored"] == "base_resume.pdf"
    assert label["renamed"] is True


def test_label_falls_back_when_the_resume_was_not_uploaded(site):
    """Somebody can drop a file into resume/ by hand. Then there is no note."""
    (site / "resume/my_cv.pdf").write_text("x")
    label = webapp.resume_label()
    assert label["name"] == "my_cv.pdf"
    assert label["renamed"] is False


def test_label_ignores_a_note_about_a_file_that_is_gone(site):
    """A stale note must not name a resume that is no longer the active one."""
    (site / "resume/base_resume.pdf").write_text("current")
    webapp.upload_note_path().write_text(
        '{"original": "old_person.docx", "stored": "base_resume.docx"}')
    assert webapp.resume_label()["name"] == "base_resume.pdf"


def test_the_note_is_never_mistaken_for_a_resume(site):
    (site / "resume/base_resume.pdf").write_text("x")
    webapp.remember_upload("chosen.pdf", site / "resume/base_resume.pdf")
    assert webapp.upload_note_path().exists()
    assert [p.name for p in webapp.resume_candidates()] == ["base_resume.pdf"]


def test_remove_clears_the_note_too(site):
    (site / "resume/base_resume.pdf").write_text("x")
    webapp.remember_upload("chosen.pdf", site / "resume/base_resume.pdf")
    with webapp.app.test_client() as client:
        assert client.post("/resume/remove").get_json()["ok"] is True
    # Otherwise the next upload's page would be labelled with the old name.
    assert not webapp.upload_note_path().exists()
