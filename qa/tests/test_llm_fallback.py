"""A busy hosted model falls back to the one on this Mac; a wrong setting does not.

OpenRouter's free models answered 429 twice and timed out once on 26 Sep 2026.
Waiting would not have helped that tailoring; the local model could."""

import pytest

from ml.resume import llm
from ml.resume.llm import Completion, ProviderBusy, ProviderUnavailable


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setenv("RESUME_PROVIDER", "openrouter")
    calls = []
    monkeypatch.setattr(llm, "_ollama", lambda s, u, cfg=None, want_json=True, model=None:
                        calls.append(model) or Completion(text="{}", model=model, provider="ollama", usage=None))
    return calls


def test_a_busy_service_hands_over_to_the_local_model(remote, monkeypatch):
    monkeypatch.setattr(llm, "_dispatch", lambda *a, **k: (_ for _ in ()).throw(ProviderBusy("openrouter returned 429")))
    monkeypatch.setattr(llm, "fallback_models", lambda cfg=None, skip="": ["gemma4:e4b"])
    answer = llm.complete("s", "u", Cfg())
    assert answer.provider == "ollama" and remote == ["gemma4:e4b"]


def test_a_wrong_key_or_model_is_reported_not_hidden(remote, monkeypatch):
    monkeypatch.setattr(llm, "_dispatch", lambda *a, **k: (_ for _ in ()).throw(
        ProviderUnavailable("does not recognise the model 'gemma4:e4b'")))
    monkeypatch.setattr(llm, "fallback_models", lambda cfg=None, skip="": ["gemma4:e4b"])
    with pytest.raises(ProviderUnavailable, match="does not recognise"):
        llm.complete("s", "u", Cfg())
    assert remote == []


def test_no_local_model_means_the_busy_error_stands(remote, monkeypatch):
    monkeypatch.setattr(llm, "_dispatch", lambda *a, **k: (_ for _ in ()).throw(ProviderBusy("timed out")))
    monkeypatch.setattr(llm, "fallback_models", lambda cfg=None, skip="": [])
    with pytest.raises(ProviderBusy):
        llm.complete("s", "u", Cfg())


def test_the_fallback_can_be_switched_off(remote, monkeypatch):
    monkeypatch.setattr(llm, "_dispatch", lambda *a, **k: (_ for _ in ()).throw(ProviderBusy("429")))
    monkeypatch.setattr(llm, "fallback_models", lambda cfg=None, skip="": ["gemma4:e4b"])
    with pytest.raises(ProviderBusy):
        llm.complete("s", "u", Cfg({"resume.fallback_to_local": False}))


def test_rate_limits_and_server_errors_count_as_busy(monkeypatch):
    class Resp:
        def __init__(self, code): self.status_code, self.text, self.ok = code, "x", code < 400
    for code, busy in ((429, True), (503, True), (401, False), (404, False)):
        monkeypatch.setattr("requests.post", lambda *a, _c=code, **k: Resp(_c))
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        with pytest.raises(ProviderUnavailable) as err:
            llm._openai_compatible("openrouter", "s", "u", Cfg())
        assert isinstance(err.value, ProviderBusy) is busy, code


def test_a_busy_cloud_model_hands_over_down_the_list(monkeypatch):
    """GLM over its limit -> MiniMax; MiniMax busy too -> the model on this Mac."""
    monkeypatch.setenv("RESUME_PROVIDER", "ollama")
    monkeypatch.setenv("RESUME_MODEL", "glm-5.3-flash:cloud")
    monkeypatch.setattr(llm, "_dispatch", lambda *a, **k: (_ for _ in ()).throw(ProviderBusy("429")))
    seen = {}
    monkeypatch.setattr(llm, "fallback_models", lambda cfg=None, skip="":
                        seen.setdefault("skip", skip) and ["minimax-m3:cloud", "gemma4:e4b"])
    tried = []

    def ollama(s, u, cfg=None, want_json=True, model=None):
        tried.append(model)
        if model == "minimax-m3:cloud":
            raise ProviderBusy("503")
        return Completion(text="{}", model=model, provider="ollama", usage=None)

    monkeypatch.setattr(llm, "_ollama", ollama)
    assert llm.complete("s", "u", Cfg()).model == "gemma4:e4b"
    assert tried == ["minimax-m3:cloud", "gemma4:e4b"]
    assert seen["skip"] == "glm-5.3-flash:cloud"          # never retried the one that failed


def test_the_list_skips_what_is_not_installed(monkeypatch):
    class Tags:
        def json(self):
            return {"models": [{"name": "gemma4:e4b"}, {"name": "glm-5.3-flash:cloud"}]}
    monkeypatch.setattr("requests.get", lambda *a, **k: Tags())
    cfg = Cfg({"resume.fallback_models": ["minimax-m3:cloud", "gemma4:e4b"],
               "resume.ollama.model": "glm-5.3-flash:cloud"})
    assert llm.fallback_models(cfg, skip="glm-5.3-flash:cloud") == ["gemma4:e4b"]


def test_another_signed_in_plan_writes_before_the_slow_local_models(remote, monkeypatch):
    tried = []

    def dispatch(provider, *a, **k):
        tried.append(provider)
        if provider == "openrouter":
            raise ProviderBusy("openrouter returned 429")
        return Completion(text="{}", model="", provider=provider, usage=None)
    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "other_plans", lambda provider, cfg=None: ["chatgpt"])
    monkeypatch.setattr(llm, "fallback_models", lambda cfg=None, skip="": ["gemma4:e4b"])
    assert llm.complete("s", "u", Cfg()).provider == "chatgpt"
    assert tried == ["openrouter", "chatgpt"] and remote == []


def test_a_busy_provider_is_not_asked_again_for_a_while(remote, monkeypatch):
    tried = []

    def dispatch(provider, *a, **k):
        tried.append(provider)
        raise ProviderBusy("usage limit reached")
    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "fallback_models", lambda cfg=None, skip="": ["gemma4:e4b"])
    llm.complete("s", "u", Cfg())
    llm.complete("s", "u", Cfg())
    assert tried == ["openrouter"] and remote == ["gemma4:e4b", "gemma4:e4b"]
