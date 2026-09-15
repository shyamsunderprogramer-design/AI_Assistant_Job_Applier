"""Choosing where the tailoring text comes from — offline, no network.

The point of this seam is that the safety rule does not move with the backend:
whatever writes the text, resume/guard.py still checks it against the base
resume. These tests cover the choosing and the parsing; the guard has its own.
"""

import pytest

from resume.llm import (
    Completion,
    ProviderUnavailable,
    complete,
    extract_json,
    model_name,
    provider_name,
)


class Cfg:
    """Config stand-in: dotted lookups with defaults."""

    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


# -- which backend ----------------------------------------------------------

def test_local_is_the_default():
    """Nobody should start paying for an API by forgetting to choose."""
    assert provider_name(None) == "ollama"
    assert provider_name(Cfg()) == "ollama"


def test_the_config_picks_the_backend():
    assert provider_name(Cfg(**{"resume.provider": "anthropic"})) == "anthropic"
    assert provider_name(Cfg(**{"resume.provider": "  OLLAMA  "})) == "ollama"


def test_the_model_reported_matches_the_backend():
    assert model_name(Cfg(**{"resume.provider": "ollama",
                             "resume.ollama.model": "qwen3.5:9b"})) == "qwen3.5:9b"
    assert model_name(Cfg(**{"resume.provider": "anthropic",
                             "resume.anthropic.model": "claude-opus-5"})) == "claude-opus-5"


def test_an_unknown_backend_says_what_the_choices_are():
    with pytest.raises(ProviderUnavailable) as raised:
        complete("sys", "user", Cfg(**{"resume.provider": "gpt-9"}))

    message = str(raised.value)
    assert "ollama" in message and "anthropic" in message


def test_a_missing_api_key_suggests_the_free_path(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ProviderUnavailable) as raised:
        complete("sys", "user", Cfg(**{"resume.provider": "anthropic"}))

    assert "resume.provider: ollama" in str(raised.value)


# -- cost ------------------------------------------------------------------

def test_a_local_completion_is_free_and_an_api_one_is_not():
    assert Completion(text="x", model="qwen3.5:9b", provider="ollama").free
    assert not Completion(text="x", model="claude-opus-5", provider="anthropic").free


# -- parsing ---------------------------------------------------------------

def test_a_code_fence_is_tolerated():
    assert extract_json('```json\n{"summary": "hi"}\n```')["summary"] == "hi"


def test_prose_around_the_object_is_tolerated():
    """Local models wrap the answer in a sentence more often than the API
    does, even when asked for JSON. Finding the outermost braces costs nothing
    and removes a whole class of avoidable failure."""
    messy = 'Sure! Here is the tailored resume:\n{"summary": "hi", "gaps": []}\nHope that helps.'
    assert extract_json(messy)["summary"] == "hi"


def test_no_object_at_all_is_an_error():
    with pytest.raises(ValueError):
        extract_json("I could not complete that request.")


# -- reaching the local server ---------------------------------------------

def test_an_unreachable_ollama_says_how_to_start_it(monkeypatch):
    import requests

    def refuse(*a, **kw):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(requests, "post", refuse)
    with pytest.raises(ProviderUnavailable) as raised:
        complete("sys", "user", Cfg(**{"resume.provider": "ollama"}))

    assert "ollama serve" in str(raised.value)


def test_a_missing_model_says_how_to_pull_it(monkeypatch):
    import requests

    class Missing:
        status_code = 404
        ok = False
        text = "model not found"

    monkeypatch.setattr(requests, "post", lambda *a, **kw: Missing())
    with pytest.raises(ProviderUnavailable) as raised:
        complete("sys", "user", Cfg(**{"resume.provider": "ollama",
                                       "resume.ollama.model": "nope:7b"}))

    assert "ollama pull nope:7b" in str(raised.value)


def test_a_local_reply_comes_back_as_a_free_completion(monkeypatch):
    import requests

    class Reply:
        status_code = 200
        ok = True

        def json(self):
            return {"message": {"content": '{"summary": "done"}'},
                    "prompt_eval_count": 40, "eval_count": 12}

    sent = {}

    def capture(url, json=None, timeout=None):
        sent.update(url=url, payload=json)
        return Reply()

    monkeypatch.setattr(requests, "post", capture)
    answer = complete("sys", "user", Cfg(**{"resume.provider": "ollama",
                                            "resume.ollama.model": "qwen3.5:9b"}))

    assert answer.free and answer.usage is None
    assert extract_json(answer.text)["summary"] == "done"
    # A short context silently truncates the resume rather than failing.
    assert sent["payload"]["options"]["num_ctx"] >= 16384
    assert sent["payload"]["format"] == "json"
    assert sent["payload"]["stream"] is False
