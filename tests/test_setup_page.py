"""The settings page holds credentials, so what it must NOT do is the test.

This repo is public and .env is gitignored, which means a fresh clone has none
of these and no obvious sign of which are missing. The page exists to say so
-- without ever becoming a place a key can be read back out of.
"""

import pytest

import webapp.app as webapp
from config.envfile import read_env


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(webapp, "resume_label", lambda: None)
    monkeypatch.setattr(webapp, "applicant_state",
                        lambda: {"ready": False, "exists": False})
    webapp.app.config["TESTING"] = True
    return tmp_path


def test_a_saved_secret_is_never_rendered_back(site):
    (site / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-supersecret-value\n"
        "MAIL_APP_PASSWORD=abcd efgh ijkl mnop\n")

    with webapp.app.test_client() as client:
        page = client.get("/setup").get_data(as_text=True)

    assert "sk-ant-supersecret-value" not in page
    assert "abcd efgh ijkl mnop" not in page
    # It says they are set, which is all anyone needs to know.
    assert "set" in page


def test_blank_keeps_a_secret_that_is_already_saved(site):
    """The box is blank every time the page loads, because it never shows the
    value. Treating blank as "delete" would wipe a working key on any save."""
    (site / ".env").write_text("ANTHROPIC_API_KEY=keep-me\n")

    with webapp.app.test_client() as client:
        body = client.post("/setup", data={"ANTHROPIC_API_KEY": "",
                                           "MAIL_ADDRESS": "me@example.com"}).get_json()

    assert body["ok"] is True
    assert read_env(site / ".env")["ANTHROPIC_API_KEY"] == "keep-me"


def test_clear_removes_a_secret(site):
    (site / ".env").write_text("ANTHROPIC_API_KEY=remove-me\n")

    with webapp.app.test_client() as client:
        client.post("/setup", data={"ANTHROPIC_API_KEY": "",
                                    "clear__ANTHROPIC_API_KEY": "on"})

    assert "ANTHROPIC_API_KEY" not in read_env(site / ".env")


def test_the_response_names_what_changed_but_not_its_value(site):
    with webapp.app.test_client() as client:
        body = client.post("/setup",
                           data={"ANTHROPIC_API_KEY": "sk-ant-brand-new"}).get_json()

    assert body["changed"] == ["ANTHROPIC_API_KEY"]
    # The response is logged by the browser and anything watching it.
    assert "sk-ant-brand-new" not in str(body)


def test_saving_creates_an_owner_only_file(site):
    import stat

    with webapp.app.test_client() as client:
        client.post("/setup", data={"ANTHROPIC_API_KEY": "sk-ant-x"})

    assert stat.S_IMODE((site / ".env").stat().st_mode) == 0o600


def test_a_visible_field_can_be_emptied(site):
    """Unlike secrets, a field whose value is on screen means what it shows."""
    (site / ".env").write_text("MAIL_ADDRESS=old@example.com\n")

    with webapp.app.test_client() as client:
        client.post("/setup", data={"MAIL_ADDRESS": ""})

    assert read_env(site / ".env")["MAIL_ADDRESS"] == ""


def test_the_page_loads_with_no_env_file_at_all(site):
    """The state a fresh clone is actually in."""
    with webapp.app.test_client() as client:
        response = client.get("/setup")
    assert response.status_code == 200
    assert b"not set" in response.data


# --- where to get each value ----------------------------------------------

def test_each_credential_links_to_the_page_that_issues_it(site):
    with webapp.app.test_client() as client:
        page = client.get("/setup").get_data(as_text=True)

    assert "https://console.anthropic.com/settings/keys" in page
    assert "https://myaccount.google.com/apppasswords" in page
    # New tab, so never without noopener.
    assert 'rel="noopener noreferrer"' in page


def test_the_repo_secrets_link_is_this_clone_s_own(monkeypatch):
    import subprocess

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout="git@github.com:someone/their-fork.git\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert webapp.github_secrets_url() == (
        "https://github.com/someone/their-fork/settings/secrets/actions")


@pytest.mark.parametrize("remote", [
    "https://github.com/a/b.git",
    "https://github.com/a/b",
    "git@github.com:a/b.git",
])
def test_both_remote_spellings_resolve(monkeypatch, remote):
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout=remote + "\n", stderr=""))
    assert webapp.github_secrets_url() == (
        "https://github.com/a/b/settings/secrets/actions")


def test_no_github_remote_means_no_link_rather_than_a_wrong_one(monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout="/srv/local.git\n", stderr=""))
    assert webapp.github_secrets_url() is None


def test_a_missing_git_binary_is_not_an_error(monkeypatch):
    import subprocess

    def boom(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    assert webapp.github_secrets_url() is None
