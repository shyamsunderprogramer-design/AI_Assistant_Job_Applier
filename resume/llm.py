"""Where the tailoring text comes from: a local model, or the Anthropic API.

Only one part of this tool has ever needed a model — rewriting a resume for a
specific posting, and drafting a cover letter. Everything else (scraping,
scoring, export, the daily digest) is offline arithmetic and string matching,
and stays that way. So this module is small on purpose: it is the single seam
between "we have a prompt" and "we have text back", and it exists so that seam
can point at a model running on this machine instead of a paid API.

Two backends, same contract:

    complete(system, user, cfg) -> Completion(text, model, provider, usage)

`ollama` talks to the server already running on this machine. It costs nothing,
sends nothing anywhere, and works with no API key and no network. `anthropic`
is the original path, kept because a 27B local model and a frontier model are
not the same thing and the choice should stay the user's.

The guard is unchanged either way. Whatever writes the text, `resume/guard.py`
still checks every skill, metric, year and entity against the base resume
before anything is saved — a local model is not trusted more than a remote one.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen3.5:9b"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"

# Every provider here except ollama bills per call. Ollama is the default and
# stays the default: a person who already pays for a chat subscription should
# not be asked to buy API credit to use this, and the job page's first offer
# is always a prompt to paste into the subscription they already have.
#
# OpenAI and xAI both serve the OpenAI chat-completions shape, so they share
# one implementation; `base_url` is what separates them. That also means any
# other service speaking the same shape -- OpenRouter, Together, LM Studio, a
# company's internal gateway -- works by setting base_url, with no new code.
PROVIDERS = {
    "ollama": {
        "label": "Ollama (on this machine)",
        "free": True,
        "env_key": None,
        "note": "Free and private. Nothing leaves this machine, and it works "
                "with no key and no network.",
    },
    "anthropic": {
        "label": "Anthropic API",
        "free": False,
        "env_key": "ANTHROPIC_API_KEY",
    },
    "openai": {
        "label": "OpenAI API",
        "free": False,
        "env_key": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
    },
    "grok": {
        "label": "xAI Grok API",
        "free": False,
        "env_key": "XAI_API_KEY",
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-4",
    },
}

# A local model is slower than an API and loads from disk on first use. The
# first call after a reboot can spend minutes just reading weights into memory.
DEFAULT_TIMEOUT_S = 900


class ProviderUnavailable(RuntimeError):
    """The chosen backend cannot be reached, with a fixable explanation."""


@dataclass(frozen=True)
class TokenUsage:
    """Token counts in the shape the cost ledger reads.

    `record_usage` reads `.input_tokens` / `.output_tokens` off whatever the
    provider returned. Anthropic's SDK gives an object with those names;
    OpenAI-compatible APIs give a dict with `prompt_tokens` /
    `completion_tokens`, and a dict answers getattr with nothing -- so the
    ledger would record a real, billed call as costing zero.
    """

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Completion:
    """One model response, with enough to bill it or to say it was free."""

    text: str
    model: str
    provider: str
    usage: object | None = None       # Anthropic usage object, or None

    @property
    def free(self) -> bool:
        """Did this call cost money? Anything but a local model does.

        This used to read `provider != "anthropic"`, which was true when
        Anthropic was the only paid backend and became a lie the moment a
        second one existed -- reporting an OpenAI call as free.
        """
        return bool(PROVIDERS.get(self.provider, {}).get("free", False))


def _setting(cfg, key: str, default):
    return default if cfg is None else cfg.get(key, default)


def provider_name(cfg=None) -> str:
    """Which backend to use. Local unless something says otherwise.

    .env wins over config.yaml, the same way DATABASE_URL does, so the
    settings page can change this without rewriting a config file whose
    comments are half its value.
    """
    chosen = os.getenv("RESUME_PROVIDER", "").strip().lower()
    return chosen or str(_setting(cfg, "resume.provider", "ollama")).strip().lower()


def default_model_for(provider: str) -> str:
    if provider == "anthropic":
        return DEFAULT_ANTHROPIC_MODEL
    if provider == "ollama":
        return DEFAULT_OLLAMA_MODEL
    return str(PROVIDERS.get(provider, {}).get("default_model", ""))


def model_name(cfg=None) -> str:
    provider = provider_name(cfg)
    chosen = os.getenv("RESUME_MODEL", "").strip()
    if chosen:
        return chosen
    return str(_setting(cfg, f"resume.{provider}.model",
                        default_model_for(provider)))


# -- the shared entry point -------------------------------------------------

def complete(system: str, user: str, cfg=None, *, max_tokens: int = 16000,
             want_json: bool = True) -> Completion:
    """Text back from whichever backend is configured."""
    provider = provider_name(cfg)
    if provider == "anthropic":
        return _anthropic(system, user, cfg, max_tokens=max_tokens)
    if provider == "ollama":
        return _ollama(system, user, cfg, want_json=want_json)
    if provider in PROVIDERS:
        return _openai_compatible(provider, system, user, cfg,
                                  max_tokens=max_tokens, want_json=want_json)
    raise ProviderUnavailable(
        f"Unknown resume.provider {provider!r}. Use one of: "
        f"{', '.join(sorted(PROVIDERS))} — ollama is local and free."
    )


# -- local ------------------------------------------------------------------

def _ollama(system: str, user: str, cfg=None, *, want_json: bool = True) -> Completion:
    import requests

    host = str(_setting(cfg, "resume.ollama.host", DEFAULT_OLLAMA_HOST)).rstrip("/")
    model = str(_setting(cfg, "resume.ollama.model", DEFAULT_OLLAMA_MODEL))
    timeout = int(_setting(cfg, "resume.ollama.timeout_seconds", DEFAULT_TIMEOUT_S))
    # A resume plus a job description plus the reply runs well past the 4k
    # default, and Ollama silently truncates the START of an overlong prompt --
    # which is where the resume sits. A short context does not fail loudly, it
    # quietly tailors against half a resume.
    num_ctx = int(_setting(cfg, "resume.ollama.num_ctx", 16384))

    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {"temperature": 0.2, "num_ctx": num_ctx},
    }
    if want_json:
        payload["format"] = "json"

    try:
        response = requests.post(f"{host}/api/chat", json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise ProviderUnavailable(
            f"Could not reach Ollama at {host}: {exc}\n"
            f"  Start it with:  ollama serve\n"
            f"  Check the model:  ollama list"
        ) from exc

    if response.status_code == 404:
        raise ProviderUnavailable(
            f"Ollama has no model called {model!r}.\n"
            f"  Pull it:  ollama pull {model}\n"
            f"  Or set resume.ollama.model in config.yaml to one you have."
        )
    if not response.ok:
        raise ProviderUnavailable(
            f"Ollama returned {response.status_code}: {response.text[:200]}")

    body = response.json()
    text = ((body.get("message") or {}).get("content") or "").strip()
    if not text:
        raise ProviderUnavailable(
            f"{model} returned nothing. It may have run out of context — "
            f"try a smaller resume.ollama.num_ctx, or a different model."
        )

    log.info("Tailored with %s locally (%s prompt / %s response tokens, no cost)",
             model, body.get("prompt_eval_count"), body.get("eval_count"))
    return Completion(text=text, model=model, provider="ollama", usage=None)


# -- remote -----------------------------------------------------------------

def _anthropic(system: str, user: str, cfg=None, *, max_tokens: int = 16000) -> Completion:
    import anthropic

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise ProviderUnavailable(
            "ANTHROPIC_API_KEY is not set, and resume.provider is 'anthropic'.\n"
            "  Either add the key to .env, or set resume.provider: ollama in "
            "config.yaml to use a model on this machine for free."
        )
    model = str(_setting(cfg, "resume.anthropic.model", DEFAULT_ANTHROPIC_MODEL))
    client = anthropic.Anthropic()

    # Streaming: a JD plus a resume plus reasoning is long, and streaming
    # avoids an HTTP timeout on a large max_tokens.
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": user}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError(f"Model declined the request: {response.stop_details}")

    text = "".join(block.text for block in response.content if block.type == "text")
    return Completion(text=text, model=model, provider="anthropic",
                      usage=getattr(response, "usage", None))


def _openai_compatible(provider: str, system: str, user: str, cfg=None, *,
                       max_tokens: int = 16000, want_json: bool = True) -> Completion:
    """OpenAI, xAI Grok, and anything else speaking chat-completions.

    One function for all of them because the request and response shapes are
    identical; only the base URL, the key and the model name differ. No SDK:
    the endpoint is a single POST, and a dependency per vendor would be three
    packages to install for a feature most people will never switch on.
    """
    import requests

    spec = PROVIDERS[provider]
    env_key = spec["env_key"]
    api_key = os.getenv(env_key, "").strip()
    if not api_key:
        raise ProviderUnavailable(
            f"{env_key} is not set, and the writing model is {provider!r}.\n"
            f"  Add it on the Settings page, or switch the model to 'ollama' "
            f"to use one on this machine for free."
        )

    base_url = str(_setting(cfg, f"resume.{provider}.base_url",
                            spec.get("base_url", ""))).rstrip("/")
    model = model_name(cfg)
    timeout = int(_setting(cfg, f"resume.{provider}.timeout_seconds", 300))

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_completion_tokens": max_tokens,
        "temperature": 0.2,
    }
    if want_json:
        payload["response_format"] = {"type": "json_object"}

    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=payload, timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ProviderUnavailable(f"Could not reach {base_url}: {exc}") from exc

    if response.status_code in (401, 403):
        raise ProviderUnavailable(
            f"{base_url} rejected the key in {env_key} ({response.status_code}). "
            f"Check it on the Settings page.")
    if response.status_code == 404 or (
            response.status_code == 400 and "model" in response.text.lower()):
        # Model names change often, and a wrong one is the most likely mistake
        # here -- so say which name was sent rather than only the status code.
        raise ProviderUnavailable(
            f"{base_url} does not recognise the model {model!r}.\n"
            f"  Set the model name on the Settings page to one your account has."
        )
    if not response.ok:
        raise ProviderUnavailable(
            f"{provider} returned {response.status_code}: {response.text[:300]}")

    body = response.json()
    choices = body.get("choices") or []
    text = (choices[0].get("message", {}).get("content") or "").strip() if choices else ""
    if not text:
        raise ProviderUnavailable(
            f"{model} returned nothing. Its reply was: {str(body)[:200]}")

    raw = body.get("usage") or {}
    usage = TokenUsage(
        input_tokens=int(raw.get("prompt_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
    ) if raw else None
    log.info("Tailored with %s via %s (%s prompt / %s response tokens)", model,
             provider, raw.get("prompt_tokens"), raw.get("completion_tokens"))
    return Completion(text=text, model=model, provider=provider, usage=usage)


# -- parsing, shared --------------------------------------------------------

def extract_json(text: str) -> dict:
    """Parse the model's JSON, tolerating a stray code fence or preamble.

    Local models are looser than the API about answering exactly in the shape
    asked for, even with format:json — a small one will occasionally wrap the
    object in prose. Finding the outermost braces costs nothing and removes a
    whole class of avoidable failure.
    """
    cleaned = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in the model response")
    return json.loads(cleaned[start : end + 1])
