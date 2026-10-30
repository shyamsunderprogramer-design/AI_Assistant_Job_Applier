"""Provider switches must not carry the primary provider's model to another CLI."""

import json
from types import SimpleNamespace

import pytest

from ml.resume import llm, plans


@pytest.mark.parametrize("target", ["chatgpt", "gemini"])
@pytest.mark.parametrize("configured", [False, True])
def test_claude_fallback_uses_target_model(monkeypatch, target, configured):
    monkeypatch.setenv("RESUME_PROVIDER", "claude-code")
    monkeypatch.setenv("RESUME_MODEL", "sonnet")
    monkeypatch.setattr(llm, "_claude_code", lambda *a: (_ for _ in ()).throw(
        llm.ProviderBusy("Claude Code: usage limit reached")))
    monkeypatch.setattr(llm, "other_plans", lambda *a: [target])
    seen = []

    def answer(system, user, model):
        seen.append(model)
        return llm.Completion(text="{}", model=model, provider=target)

    monkeypatch.setattr(plans, target, answer)
    expected = "fictional-target-model" if configured else ""
    cfg = {f"resume.{target}.model": expected} if configured else {}
    assert llm.complete("fictional system", "fictional resume", cfg).provider == target
    assert seen == [expected]


@pytest.mark.parametrize("target", ["chatgpt", "gemini"])
def test_primary_plan_keeps_environment_model_override(monkeypatch, target):
    monkeypatch.setenv("RESUME_PROVIDER", target)
    monkeypatch.setenv("RESUME_MODEL", "fictional-primary-model")
    monkeypatch.setattr(plans, target, lambda s, u, model:
                        llm.Completion(text="{}", model=model, provider=target))
    assert llm.complete("s", "u", {}).model == "fictional-primary-model"


@pytest.mark.parametrize("configured", [False, True])
def test_fallback_to_claude_uses_claude_model(monkeypatch, tmp_path, configured):
    monkeypatch.setenv("RESUME_PROVIDER", "chatgpt")
    monkeypatch.setenv("RESUME_MODEL", "fictional-primary-model")
    monkeypatch.setattr(plans, "chatgpt", lambda *a: (_ for _ in ()).throw(
        llm.ProviderBusy("ChatGPT: usage limit reached")))
    monkeypatch.setattr(llm, "other_plans", lambda *a: ["claude-code"])
    monkeypatch.setattr(llm, "claude_code_path", lambda: "/fictional/claude")
    monkeypatch.setattr(llm, "__file__", str(tmp_path / "ml/resume/llm.py"))
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"result": "{}"}))

    monkeypatch.setattr("subprocess.run", run)
    expected = "fictional-claude-model" if configured else llm.DEFAULT_CLAUDE_CODE_MODEL
    cfg = {"resume.claude-code.model": expected} if configured else {}
    assert llm.complete("s", "u", cfg).model == expected
    assert commands[0][commands[0].index("--model") + 1] == expected


def test_fallback_log_names_each_failure_without_echoing_private_output(monkeypatch, caplog):
    monkeypatch.setenv("RESUME_PROVIDER", "claude-code")
    monkeypatch.setattr(llm, "other_plans", lambda *a: ["chatgpt", "gemini"])
    monkeypatch.setattr(llm, "fallback_models", lambda *a, **k: ["fictional-local-model"])
    failures = {
        "claude-code": llm.ProviderBusy("Claude Code: usage limit reached; PRIVATE_PROMPT"),
        "chatgpt": llm.ProviderUnavailable("ChatGPT: model not supported; PRIVATE_TOKEN"),
        "gemini": llm.ProviderUnavailable("Gemini: not signed in; PRIVATE_EMAIL"),
    }

    def dispatch(provider, *args, **kwargs):
        raise failures[provider]

    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "_ollama", lambda *a, **k:
                        llm.Completion(text="{}", model=k["model"], provider="ollama"))
    assert llm.complete("s", "u", {}).provider == "ollama"
    assert "claude-code could not finish: usage limit" in caplog.text
    assert "chatgpt could not finish: model was rejected" in caplog.text
    assert "gemini could not finish: sign-in or authentication failed" in caplog.text
    assert "Trying fictional-local-model" in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_claude_default_model_does_not_leak_without_environment_override(monkeypatch):
    monkeypatch.setenv("RESUME_PROVIDER", "claude-code")
    monkeypatch.delenv("RESUME_MODEL", raising=False)
    monkeypatch.setattr(plans, "chatgpt", lambda s, u, model:
                        llm.Completion(text="{}", model=model, provider="chatgpt"))
    answer = llm._dispatch("chatgpt", "s", "u", {}, max_tokens=10, want_json=True)
    assert answer.model == ""
