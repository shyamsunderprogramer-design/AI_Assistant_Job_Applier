"""Claude, ChatGPT and Gemini on the person's own plan: status, environment, dispatch."""

import subprocess

import pytest

from ml.resume import plans


class Done:
    def __init__(self, out="", err="", code=0):
        self.stdout, self.stderr, self.returncode = out, err, code


def test_node_tools_are_found_from_a_background_service(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    env = plans._env()
    assert "/opt/homebrew/bin" in env["PATH"].split(":") and "OPENAI_API_KEY" not in env


@pytest.mark.parametrize("said,code,signed", [
    ("Logged in using ChatGPT", 0, True),
    ("Not logged in", 1, False),
])
def test_chatgpt_sign_in_is_read_from_codex(monkeypatch, said, code, signed):
    monkeypatch.setattr(plans, "tool_path", lambda b: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done(out=said, code=code))
    assert plans.status("chatgpt")["signed_in"] is signed


def test_a_missing_tool_offers_install(monkeypatch):
    monkeypatch.setattr(plans, "tool_path", lambda b: None)
    s = plans.status("gemini")
    assert not s["installed"] and not s["signed_in"]


def test_a_signed_out_tool_says_how_to_sign_in(monkeypatch, tmp_path):
    from ml.resume.llm import ProviderUnavailable
    monkeypatch.setattr(plans, "tool_path", lambda b: "/bin/gemini")
    monkeypatch.setattr(plans, "WORKDIR", tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done(err="Please login first", code=1))
    with pytest.raises(ProviderUnavailable, match="Sign in"):
        plans.gemini("system", "user")


def test_the_writer_reaches_the_chosen_plan(monkeypatch):
    from ml.resume import llm
    monkeypatch.setattr(plans, "chatgpt", lambda s, u, m="": llm.Completion("ok", "m", "chatgpt", None))
    assert llm._dispatch("chatgpt", "s", "u", None, max_tokens=10, want_json=False).provider == "chatgpt"
