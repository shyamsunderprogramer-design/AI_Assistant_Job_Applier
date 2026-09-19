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


# --- choosing who writes ---------------------------------------------------

def test_free_options_are_listed_before_paid_ones(site):
    """Somebody who already pays for a chat subscription should not be nudged
    into buying API credit as well."""
    options = webapp.writing_model_state()["options"]
    free = [o["free"] for o in options]
    assert free[0] is True
    # Every free option comes before every paid one: no True after a False.
    assert free == sorted(free, reverse=True)
    assert "ollama" in [o["name"] for o in options if o["free"]]


def test_a_provider_with_no_key_is_marked_not_ready(site):
    (site / ".env").write_text("OPENAI_API_KEY=sk-test\n")
    ready = {o["name"]: o["ready"] for o in webapp.writing_model_state()["options"]}
    assert ready["openai"] is True
    assert ready["grok"] is False
    assert ready["ollama"] is True          # needs no key at all


def test_choosing_a_provider_writes_it_to_env(site):
    with webapp.app.test_client() as client:
        body = client.post("/setup", data={"RESUME_PROVIDER": "ollama"}).get_json()
    assert body["ok"] is True
    assert read_env(site / ".env")["RESUME_PROVIDER"] == "ollama"


def test_an_unknown_provider_is_refused(site):
    with webapp.app.test_client() as client:
        response = client.post("/setup", data={"RESUME_PROVIDER": "definitely-not"})
    assert response.status_code == 400
    assert "RESUME_PROVIDER" not in read_env(site / ".env")


def test_a_blank_model_means_the_provider_default(site):
    (site / ".env").write_text("RESUME_MODEL=gpt-4o-mini\n")
    with webapp.app.test_client() as client:
        client.post("/setup", data={"RESUME_PROVIDER": "ollama", "RESUME_MODEL": ""})
    # Removed, not stored as an empty model name that nothing could serve.
    assert "RESUME_MODEL" not in read_env(site / ".env")


# --- the second button on the job page ------------------------------------

def test_the_second_button_is_free_when_a_local_model_writes(site, monkeypatch):
    """It used to be hard-wired to the paid API: it appeared only with an
    Anthropic key and always said "pay". A local model does the same job for
    nothing, so nobody should be asked to pay for what their Mac can do."""
    monkeypatch.setenv("RESUME_PROVIDER", "ollama")
    monkeypatch.setenv("RESUME_MODEL", "gemma4:e4b")
    state = webapp.writer_state()
    assert state["free"] is True
    assert state["ready"] is True          # needs no key
    assert "pay" not in state["label"].lower()
    assert state["model"] == "gemma4:e4b"


def test_a_paid_provider_still_says_so(site, monkeypatch):
    monkeypatch.setenv("RESUME_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    state = webapp.writer_state()
    assert state["free"] is False
    assert "pay" in state["label"].lower()


def test_a_paid_provider_with_no_key_cannot_run(site, monkeypatch):
    """Offering a button that can only fail is worse than not offering it."""
    monkeypatch.setenv("RESUME_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert webapp.writer_state()["ready"] is False


def test_a_keyless_gateway_is_ready_only_while_it_is_running(site, monkeypatch):
    """Needing no key makes it configured, not reachable. A gateway that was
    never started would otherwise show a button that fails on press."""
    monkeypatch.setenv("RESUME_PROVIDER", "omniroute")

    monkeypatch.setattr(webapp, "service_up", lambda url, timeout=0.4: True)
    up = webapp.writer_state()
    assert up["ready"] is True and up["free"] is True
    assert up["needs_starting"] is False

    monkeypatch.setattr(webapp, "service_up", lambda url, timeout=0.4: False)
    down = webapp.writer_state()
    assert down["ready"] is False
    assert down["needs_starting"] is True


def test_a_local_model_that_is_switched_off_is_not_ready(site, monkeypatch):
    monkeypatch.setenv("RESUME_PROVIDER", "ollama")
    monkeypatch.setattr(webapp, "service_up", lambda url, timeout=0.4: False)
    assert webapp.writer_state()["ready"] is False


def test_service_up_says_no_for_a_closed_port():
    # Port 1 is reserved and nothing listens there.
    assert webapp.service_up("http://localhost:1", timeout=0.2) is False
    assert webapp.service_up("") is False
    assert webapp.service_up("not a url") is False
