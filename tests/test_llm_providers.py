"""More than one paid backend, without quietly telling anyone it was free.

Ollama stays the default and stays free. The point of the others is choice --
someone may already pay OpenAI or xAI and would rather use that than buy a
second kind of credit.
"""

import pytest
import requests

from resume.cost import Ledger, record_usage
from resume.llm import (
    PROVIDERS,
    Completion,
    ProviderUnavailable,
    complete,
    model_name,
    provider_name,
)


def test_the_default_is_the_free_local_one(monkeypatch):
    monkeypatch.delenv("RESUME_PROVIDER", raising=False)
    assert provider_name(None) == "ollama"
    assert PROVIDERS["ollama"]["free"] is True


@pytest.mark.parametrize("provider,free", [
    ("ollama", True), ("anthropic", False), ("openai", False), ("grok", False),
])
def test_only_the_local_model_reports_free(provider, free):
    """This read `provider != "anthropic"` and became a lie the moment a
    second paid backend existed."""
    assert Completion(text="x", model="m", provider=provider).free is free


def test_an_unknown_provider_is_not_assumed_free():
    assert Completion(text="x", model="m", provider="something-new").free is False


def test_env_overrides_the_config_file(monkeypatch):
    """So the settings page never has to rewrite a commented config.yaml."""
    monkeypatch.setenv("RESUME_PROVIDER", "openai")
    monkeypatch.setenv("RESUME_MODEL", "gpt-4o-mini")
    assert provider_name(None) == "openai"
    assert model_name(None) == "gpt-4o-mini"


def test_a_missing_key_says_which_one_and_offers_the_free_route(monkeypatch):
    monkeypatch.setenv("RESUME_PROVIDER", "grok")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailable) as err:
        complete("sys", "user", None)
    assert "XAI_API_KEY" in str(err.value)
    assert "ollama" in str(err.value)


def test_an_unknown_provider_lists_the_real_ones(monkeypatch):
    monkeypatch.setenv("RESUME_PROVIDER", "nonesuch")
    with pytest.raises(ProviderUnavailable) as err:
        complete("sys", "user", None)
    assert "ollama" in str(err.value)


class _Response:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload or {}, text
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


def test_openai_shape_is_read_and_billed(monkeypatch):
    monkeypatch.setenv("RESUME_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sent = {}

    def fake_post(url, **kwargs):
        sent["url"], sent["json"] = url, kwargs["json"]
        sent["auth"] = kwargs["headers"]["Authorization"]
        return _Response(payload={
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 340},
        })

    monkeypatch.setattr(requests, "post", fake_post)
    answer = complete("system text", "user text", None)

    assert sent["url"] == "https://api.openai.com/v1/chat/completions"
    assert sent["auth"] == "Bearer sk-test"
    assert answer.text == '{"ok": true}'
    assert answer.free is False

    # The ledger reads attributes, and a raw dict would have billed zero.
    cost = record_usage(Ledger(cap_usd=10), answer.model, answer.usage)
    assert cost.input_tokens == 1200 and cost.output_tokens == 340
    assert cost.usd > 0


def test_grok_goes_to_xai(monkeypatch):
    monkeypatch.setenv("RESUME_PROVIDER", "grok")
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        return _Response(payload={"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setattr(requests, "post", fake_post)
    assert complete("s", "u", None).text == "hi"
    assert seen["url"] == "https://api.x.ai/v1/chat/completions"


def test_a_rejected_key_says_so_plainly(monkeypatch):
    monkeypatch.setenv("RESUME_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-wrong")
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Response(401, text="nope"))
    with pytest.raises(ProviderUnavailable) as err:
        complete("s", "u", None)
    assert "OPENAI_API_KEY" in str(err.value)


def test_a_wrong_model_name_says_which_name_was_sent(monkeypatch):
    """Model names change often; this is the likeliest mistake."""
    monkeypatch.setenv("RESUME_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("RESUME_MODEL", "gpt-does-not-exist")
    monkeypatch.setattr(requests, "post",
                        lambda url, **kw: _Response(404, text="no such model"))
    with pytest.raises(ProviderUnavailable) as err:
        complete("s", "u", None)
    assert "gpt-does-not-exist" in str(err.value)
