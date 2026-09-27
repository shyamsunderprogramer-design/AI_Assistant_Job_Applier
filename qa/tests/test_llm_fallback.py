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
    monkeypatch.setattr(llm, "local_model", lambda cfg=None: "gemma4:e4b")
    answer = llm.complete("s", "u", Cfg())
    assert answer.provider == "ollama" and remote == ["gemma4:e4b"]


def test_a_wrong_key_or_model_is_reported_not_hidden(remote, monkeypatch):
    monkeypatch.setattr(llm, "_dispatch", lambda *a, **k: (_ for _ in ()).throw(
        ProviderUnavailable("does not recognise the model 'gemma4:e4b'")))
    monkeypatch.setattr(llm, "local_model", lambda cfg=None: "gemma4:e4b")
    with pytest.raises(ProviderUnavailable, match="does not recognise"):
        llm.complete("s", "u", Cfg())
    assert remote == []


def test_no_local_model_means_the_busy_error_stands(remote, monkeypatch):
    monkeypatch.setattr(llm, "_dispatch", lambda *a, **k: (_ for _ in ()).throw(ProviderBusy("timed out")))
    monkeypatch.setattr(llm, "local_model", lambda cfg=None: None)
    with pytest.raises(ProviderBusy):
        llm.complete("s", "u", Cfg())


def test_the_fallback_can_be_switched_off(remote, monkeypatch):
    monkeypatch.setattr(llm, "_dispatch", lambda *a, **k: (_ for _ in ()).throw(ProviderBusy("429")))
    monkeypatch.setattr(llm, "local_model", lambda cfg=None: "gemma4:e4b")
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
