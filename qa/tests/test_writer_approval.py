"""A1.1.1 (10 Nov 2026): only AI writers verified to cost nothing per use write by themselves (ml/resume/writers).

The person's own settings, as they stand on this Mac: Claude Code (their plan) chosen, and as fallbacks two Ollama
cloud models -- which Ollama bills per token -- then a model on this Mac; ChatGPT (Codex) and Gemini signed in.
Every check here stands in for the writers themselves: no model, no CLI, no network."""

import pytest

from ml.resume import cost, llm, plans, writers
from ml.resume.llm import Completion, ProviderBusy

REAL_PLAN = writers._claude_plan          # before conftest stands it in for each test
REAL_OTHER_PLANS = llm.other_plans        # conftest makes it [] for every test: here the real one is judged


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


# As in the person's config.yaml (backend/config/config.yaml, read 10 Nov 2026), and their .env's provider.
SETTINGS = {"resume.fallback_models": ["glm-5.3-flash:cloud", "minimax-m3:cloud", "gemma4:e4b"],
            "resume.ollama.model": "glm-5.3-flash:cloud", "resume.max_spend_per_run_usd": 2.0}


@pytest.fixture
def mac(monkeypatch):
    """Ollama up with the cloud models pulled and one local model; both other plans signed in; every call
    written down instead of made."""
    class Tags:
        def json(self):
            return {"models": [{"name": n} for n in ("glm-5.3-flash:cloud", "minimax-m3:cloud", "gemma4:e4b")]}
    monkeypatch.setattr("requests.get", lambda *a, **k: Tags())
    monkeypatch.setattr(plans, "status", lambda tool: {"signed_in": True})
    monkeypatch.setattr(llm, "other_plans", REAL_OTHER_PLANS)
    monkeypatch.setenv("RESUME_PROVIDER", "claude-code")
    monkeypatch.delenv("RESUME_MODEL", raising=False)
    llm._BUSY_UNTIL.clear()
    calls = []

    def dispatch(provider, system, user, cfg, *, max_tokens, want_json):
        calls.append(provider)
        if provider == "claude-code":
            raise ProviderBusy("Claude Code: usage limit reached")
        return Completion(text="{}", model=provider, provider=provider, usage=None)

    def ollama(system, user, cfg=None, *, want_json=True, model=None):
        calls.append(f"ollama:{model}")
        return Completion(text="{}", model=model, provider="ollama", usage=None)
    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "_ollama", ollama)
    yield calls
    llm._BUSY_UNTIL.clear()


def test_a_busy_claude_falls_back_only_to_the_model_on_this_mac(mac):
    answer = llm.complete("s", "u", Cfg(SETTINGS))
    assert answer.model == "gemma4:e4b"
    assert mac == ["claude-code", "ollama:gemma4:e4b"]          # never ChatGPT, Gemini or a cloud model


def test_the_fallback_list_keeps_only_local_models_and_the_settings_stay_as_they_were(mac):
    cfg = Cfg(SETTINGS)
    before = dict(cfg)
    assert llm.fallback_models(cfg) == ["gemma4:e4b"]
    assert llm.other_plans("claude-code", cfg) == []
    assert dict(cfg) == before                                   # filtered, never changed


def test_naming_a_writer_turns_it_back_on(mac):
    cfg = Cfg({**SETTINGS, "resume.allow_writers": ["chatgpt", "ollama-cloud"]})
    assert llm.other_plans("claude-code", cfg) == ["chatgpt"]
    assert llm.fallback_models(cfg) == ["glm-5.3-flash:cloud", "minimax-m3:cloud", "gemma4:e4b"]
    assert llm.complete("s", "u", cfg).provider == "chatgpt"


def test_no_approved_writer_left_means_nothing_is_written(mac, monkeypatch):
    class Tags:
        def json(self):
            return {"models": [{"name": "glm-5.3-flash:cloud"}, {"name": "minimax-m3:cloud"}]}
    monkeypatch.setattr("requests.get", lambda *a, **k: Tags())          # no model on this Mac
    with pytest.raises(ProviderBusy):
        llm.complete("s", "u", Cfg(SETTINGS))
    assert mac == ["claude-code"]                                         # nothing else was asked


@pytest.mark.parametrize("status", [{}, {"loggedIn": False}, {"loggedIn": True, "authMethod": "apiKey"},
                                    {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"}])
def test_claude_not_on_a_plan_is_refused_before_anything_is_sent(mac, monkeypatch, status):
    monkeypatch.setattr(writers, "_claude_plan", lambda: status)
    with pytest.raises(writers.WriterNotApproved):
        llm.complete("s", "u", Cfg(SETTINGS))
    assert mac == []


@pytest.mark.parametrize("provider", ["chatgpt", "gemini", "omniroute"])
def test_an_unverified_writer_chosen_in_settings_is_refused(mac, monkeypatch, provider):
    monkeypatch.setenv("RESUME_PROVIDER", provider)
    with pytest.raises(writers.WriterNotApproved, match="Not written"):
        llm.complete("s", "u", Cfg(SETTINGS))
    assert mac == [] and writers.primary_problem(Cfg(SETTINGS))
    assert writers.primary_problem(Cfg({**SETTINGS, "resume.allow_writers": [provider]})) is None


@pytest.mark.parametrize("model,host,ok", [
    ("gemma4:e4b", "http://localhost:11434", True),
    ("qwen3.5:9b", "http://127.0.0.1:11434", True),
    ("glm-5.3-flash:cloud", "http://localhost:11434", False),
    ("gpt-oss:120b-cloud", "http://localhost:11434", False),
    ("glm:cloud-q4", "http://localhost:11434", False),
    ("registry.local:5000/glm:cloud", "http://localhost:11434", False),
    ("registry.local:5000/gemma4:e4b", "http://localhost:11434", True),
    ("gemma4:e4b", "http://gpu-box.example.com:11434", False),
])
def test_ollama_is_free_only_for_a_model_on_this_mac(model, host, ok):
    assert writers.verdict("ollama", Cfg({"resume.ollama.host": host}), model=model)[0] is ok


def test_a_writer_that_bills_writes_only_inside_a_spending_limit(mac, monkeypatch):
    monkeypatch.setenv("RESUME_PROVIDER", "anthropic")
    with pytest.raises(writers.WriterNotApproved, match="bills per use"):
        llm.complete("s", "u", Cfg(SETTINGS))
    assert mac == []
    monkeypatch.setattr(cost, "check_billed_call", lambda *a, **k: cost._Hold(0.01, []))
    monkeypatch.setattr(cost, "settle_billed_call", lambda *a, **k: None)
    with cost.budget(cost.Ledger(cap_usd=2.0)):
        assert llm.complete("s", "u", Cfg(SETTINGS)).provider == "anthropic"


def test_only_verified_writers_count_as_free():
    assert writers.free_writer("claude-code", Cfg()) and writers.free_writer("ollama", Cfg())
    for name in ("chatgpt", "gemini", "omniroute", "anthropic", "openai", "openrouter", "grok"):
        assert not writers.free_writer(name, Cfg()), name


def test_the_claude_plan_is_read_from_claude_itself_and_not_every_call(monkeypatch):
    """The real reading (conftest's stand-in put aside), with `claude auth status` stood in for."""
    import subprocess
    ran = []

    class Done:
        stdout = '{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty"}'
    monkeypatch.setattr(writers, "_PLAN", {"at": -1e9, "status": {}})
    monkeypatch.setattr(llm, "claude_code_path", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: ran.append((cmd, k["env"])) or Done())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key-that-must-not-be-passed")
    assert REAL_PLAN()["authMethod"] == "claude.ai"
    REAL_PLAN()
    assert len(ran) == 1 and ran[0][0][1:] == ["auth", "status"]       # once a minute
    assert "ANTHROPIC_API_KEY" not in ran[0][1]
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (_ for _ in ()).throw(OSError("gone")))
    writers._PLAN["at"] = -1e9
    assert REAL_PLAN() == {}                                            # unreadable: never taken for a plan


def test_writers_named_on_one_line_are_each_allowed():
    cfg = Cfg({"resume.allow_writers": "chatgpt, gemini"})
    assert writers.verdict("chatgpt", cfg)[0] and writers.verdict("gemini", cfg)[0]


def test_the_assistant_chat_starts_claude_only_on_a_plan(monkeypatch):
    from backend.assistant import chat
    started = []
    monkeypatch.setattr(chat, "claude_path", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(writers, "_claude_plan", lambda: {"loggedIn": True, "authMethod": "console"})
    turn = chat.start("hello", runner=lambda *a, **k: started.append(a))
    assert turn.done and "Not started" in turn.error and started == []
