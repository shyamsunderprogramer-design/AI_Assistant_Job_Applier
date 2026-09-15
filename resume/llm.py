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

# A local model is slower than an API and loads from disk on first use. The
# first call after a reboot can spend minutes just reading weights into memory.
DEFAULT_TIMEOUT_S = 900


class ProviderUnavailable(RuntimeError):
    """The chosen backend cannot be reached, with a fixable explanation."""


@dataclass(frozen=True)
class Completion:
    """One model response, with enough to bill it or to say it was free."""

    text: str
    model: str
    provider: str
    usage: object | None = None       # Anthropic usage object, or None

    @property
    def free(self) -> bool:
        return self.provider != "anthropic"


def _setting(cfg, key: str, default):
    return default if cfg is None else cfg.get(key, default)


def provider_name(cfg=None) -> str:
    """Which backend to use. Local unless the config says otherwise."""
    return str(_setting(cfg, "resume.provider", "ollama")).strip().lower()


def model_name(cfg=None) -> str:
    if provider_name(cfg) == "anthropic":
        return str(_setting(cfg, "resume.anthropic.model", DEFAULT_ANTHROPIC_MODEL))
    return str(_setting(cfg, "resume.ollama.model", DEFAULT_OLLAMA_MODEL))


# -- the shared entry point -------------------------------------------------

def complete(system: str, user: str, cfg=None, *, max_tokens: int = 16000,
             want_json: bool = True) -> Completion:
    """Text back from whichever backend is configured."""
    provider = provider_name(cfg)
    if provider == "anthropic":
        return _anthropic(system, user, cfg, max_tokens=max_tokens)
    if provider == "ollama":
        return _ollama(system, user, cfg, want_json=want_json)
    raise ProviderUnavailable(
        f"Unknown resume.provider {provider!r}. Use 'ollama' (local, free) "
        f"or 'anthropic' (API, costs money)."
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
