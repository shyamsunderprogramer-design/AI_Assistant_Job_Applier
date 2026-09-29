"""Claude as a model through Claude Code: the person's plan, no API key, no tools."""

import json
import subprocess

import pytest

from ml.resume import llm
from ml.resume.llm import ProviderBusy, ProviderUnavailable


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class Done:
    def __init__(self, body=None, code=0, stderr=""):
        self.stdout = json.dumps(body) if body is not None else ""
        self.returncode, self.stderr = code, stderr


@pytest.fixture
def claude(monkeypatch):
    monkeypatch.setenv("RESUME_PROVIDER", "claude-code")
    monkeypatch.delenv("RESUME_MODEL", raising=False)
    monkeypatch.setattr(llm, "claude_code_path", lambda: "/bin/claude")
    calls = []

    def answer(body=None, code=0, stderr=""):
        def run(cmd, **kw):
            calls.append((cmd, kw))
            return Done(body, code, stderr)
        monkeypatch.setattr(subprocess, "run", run)
    return answer, calls


def test_it_is_a_model_with_no_tools_and_no_api_key(claude, monkeypatch):
    answer, calls = claude
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    answer({"result": '{"summary": "x"}', "is_error": False, "usage": {}})
    got = llm.complete("SYSTEM", "the resume and posting", Cfg())
    cmd, kw = calls[0]
    assert got.text == '{"summary": "x"}' and got.provider == "claude-code" and got.free
    assert cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "SYSTEM"
    assert "--no-session-persistence" in cmd and "--model" not in cmd
    assert kw["input"] == "the resume and posting"                 # stdin, not argv
    assert "ANTHROPIC_API_KEY" not in kw["env"]


def test_a_chosen_model_is_passed_on(claude, monkeypatch):
    answer, calls = claude
    monkeypatch.setenv("RESUME_MODEL", "sonnet")
    answer({"result": "ok", "is_error": False})
    assert llm.complete("s", "u", Cfg()).model == "sonnet"
    assert calls[0][0][calls[0][0].index("--model") + 1] == "sonnet"


def test_a_plan_limit_is_busy_so_the_backups_take_over(claude, monkeypatch):
    answer, _ = claude
    answer({"result": "Claude usage limit reached", "is_error": True}, code=1)
    with pytest.raises(ProviderBusy):
        llm._claude_code("s", "u", Cfg())
    monkeypatch.setattr(llm, "fallback_models", lambda cfg=None, skip="": ["glm-5.3-flash:cloud"])
    monkeypatch.setattr(llm, "_ollama", lambda s, u, cfg=None, want_json=True, model=None:
                        llm.Completion(text="{}", model=model, provider="ollama"))
    assert llm.complete("s", "u", Cfg()).model == "glm-5.3-flash:cloud"


def test_not_signed_in_is_said_plainly_and_not_hidden(claude):
    answer, _ = claude
    answer({"result": "Invalid API key · Please run /login", "is_error": True}, code=1)
    with pytest.raises(ProviderUnavailable, match="not signed in") as err:
        llm.complete("s", "u", Cfg())
    assert not isinstance(err.value, ProviderBusy)


def test_it_shows_in_settings_as_a_keyless_choice():
    spec = llm.PROVIDERS["claude-code"]
    assert spec["env_key"] is None and spec["free"]
