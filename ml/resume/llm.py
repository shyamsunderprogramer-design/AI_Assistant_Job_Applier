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

The guard is unchanged either way. Whatever writes the text, `ml/resume/guard.py`
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
    "claude-code": {
        "label": "Claude, through Claude Code (your plan)",
        "free": True,           # no per-call bill: it is the person's own plan
        "env_key": None,
        "default_model": "",    # "" = the plan's default; or sonnet, opus, haiku
        "note": "Claude, signed in the way you already use Claude Code -- no "
                "API key and no credit to buy. Each resume uses some of your "
                "plan's usage. The resume and posting go to Anthropic.",
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
    "omniroute": {
        "label": "OmniRoute (self-hosted gateway)",
        "free": True,           # keyless by default and runs on this machine
        # A self-hosted gateway starts keyless, but it can be configured to
        # require one -- so the key is accepted and sent when present, and
        # never demanded.
        "env_key": "OMNIROUTE_API_KEY",
        "key_optional": True,
        "base_url": "http://localhost:20128/v1",
        # "auto" is the gateway's own routing combo: it picks a provider,
        # falls back when one fails, and the keyless free providers are wired
        # into it on a fresh install.
        "default_model": "auto",
        "note": "An MIT gateway you run yourself, reaching many providers "
                "through one endpoint -- including free tiers. Free like "
                "Ollama, but the work happens on someone else's hardware, so "
                "it does not need 18GB of your RAM. Your resume does leave "
                "this machine: it goes to whichever provider it routes to.",
    },
    "openrouter": {
        "label": "OpenRouter (one key, many models)",
        "free": False,          # pay-as-you-go by default -- but see the note
        "env_key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "qwen/qwen3.8-27b:free",
        "note": "One key reaches ~450 models from most vendors, and about 25 "
                "of them cost nothing -- any model id ending ':free'. Those "
                "run on their hardware, so they are free without being slow "
                "on yours.",
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


class ProviderBusy(ProviderUnavailable):
    """The service is there but not answering now: rate-limited, overloaded,
    timed out. Unlike a wrong key or model name, waiting or going elsewhere helps."""


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
    """Text back from whichever backend is configured.

    When a hosted service is busy -- OpenRouter's free models turned away
    three requests in a row on 26 Sep 2026 -- the model on this Mac writes it
    instead, if one is installed. A wrong key or model name is NOT covered:
    that needs fixing, and falling back would hide it.
    """
    provider = provider_name(cfg)
    try:
        return _dispatch(provider, system, user, cfg, max_tokens=max_tokens, want_json=want_json)
    except ProviderBusy as exc:
        if not _setting(cfg, "resume.fallback_to_local", True):
            raise
        failed = model_name(cfg) if provider == "ollama" else ""
        last = exc
        for backup in fallback_models(cfg, skip=failed):
            log.warning("%s is busy (%s) — writing with %s instead",
                        failed or provider, str(last).split(":")[0][:80], backup)
            try:
                return _ollama(system, user, cfg, want_json=want_json, model=backup)
            except ProviderBusy as again:
                failed, last = backup, again
        raise last


def _dispatch(provider, system, user, cfg, *, max_tokens, want_json) -> Completion:
    if provider == "claude-code":
        return _claude_code(system, user, cfg)
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


# Tried in order when a hosted service is busy: what worked here before first.
LOCAL_PREFERENCE = ("gemma4:e4b", "qwen3.5:9b")


def fallback_models(cfg=None, skip: str = "") -> list[str]:
    """Ollama models to try, in order, when the first choice is busy.

    `resume.fallback_models` first -- a second cloud model belongs there, so
    one cloud model's bad hour does not drop straight to the small model on
    this Mac -- then the configured Ollama model, then what worked here
    before. Only installed ones (a cloud model is "installed" once pulled),
    never the one that just failed, and none at all if Ollama is not up.
    """
    import requests

    host = str(_setting(cfg, "resume.ollama.host", DEFAULT_OLLAMA_HOST)).rstrip("/")
    try:
        installed = [m["name"] for m in requests.get(f"{host}/api/tags", timeout=3).json()["models"]]
    except Exception:
        return []
    listed = _setting(cfg, "resume.fallback_models", []) or []
    wanted = [*[str(m) for m in listed], str(_setting(cfg, "resume.ollama.model", "")),
              *LOCAL_PREFERENCE]
    return [m for m in dict.fromkeys(wanted) if m and m != skip and m in installed]


def local_model(cfg=None) -> str | None:
    """An installed Ollama model to fall back to, or None if Ollama is not up."""
    found = fallback_models(cfg)
    return found[0] if found else None


# -- local ------------------------------------------------------------------

def _ollama(system: str, user: str, cfg=None, *, want_json: bool = True,
            model: str | None = None) -> Completion:
    import requests

    host = str(_setting(cfg, "resume.ollama.host", DEFAULT_OLLAMA_HOST)).rstrip("/")
    model = model or model_name(cfg)   # not the config directly: RESUME_MODEL must win
    timeout = int(_setting(cfg, "resume.ollama.timeout_seconds", DEFAULT_TIMEOUT_S))
    # A resume plus a job description plus the reply runs well past the 4k
    # default, and Ollama silently truncates the START of an overlong prompt --
    # which is where the resume sits. A short context does not fail loudly, it
    # quietly tailors against half a resume.
    num_ctx = int(_setting(cfg, "resume.ollama.num_ctx", 24576))

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
        # A thinking model (qwen3.5) asked for JSON spends the whole reply
        # thinking and answers with an empty string -- the "returned nothing"
        # that cost a SpaceX resume its tailoring on 27 Sep 2026. The answer is
        # the JSON; the thinking is not wanted. Not for a cloud model: GLM
        # told not to think writes its thinking into the answer instead,
        # and with thinking on it keeps it apart, where it belongs.
        if not model.endswith(":cloud"):
            payload["think"] = False

    try:
        response = requests.post(f"{host}/api/chat", json=payload, timeout=timeout)
    except requests.Timeout as exc:
        raise ProviderBusy(f"{model} timed out after {timeout}s") from exc
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
    if response.status_code == 429 or response.status_code >= 500:
        # A cloud model over its usage limit, or overloaded: another may answer.
        raise ProviderBusy(f"{model} returned {response.status_code}: {response.text[:200]}")
    if not response.ok:
        raise ProviderUnavailable(
            f"Ollama returned {response.status_code}: {response.text[:200]}")

    body = response.json()
    text = ((body.get("message") or {}).get("content") or "").strip()
    if not text:
        raise ProviderBusy(
            f"{model} returned nothing. It may have run out of context — "
            f"try a smaller resume.ollama.num_ctx, or a different model."
        )

    if body.get("done_reason") == "length":
        log.warning("%s ran out of room (num_ctx %d) and its reply was cut off — "
                    "the complete part will be used", model, num_ctx)
    log.info("Tailored with %s %s (%s prompt / %s response tokens, no cost)",
             model, "on Ollama Cloud" if model.endswith(":cloud") else "locally",
             body.get("prompt_eval_count"), body.get("eval_count"))
    return Completion(text=text, model=model, provider="ollama", usage=None)


# -- Claude Code ---------------------------------------------------------------
# Claude as a model, the way Ollama serves GLM: `claude -p`, the person's own
# sign-in, one prompt in and text out. Boxed in to be a model and nothing else.

# Claude Code bills an API key over the sign-in whenever it sees one, and the
# app has .env loaded -- the chat met "Credit balance is too low" that way.
CLAUDE_CODE_HIDDEN_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                          "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")


def claude_code_path() -> str | None:
    import shutil
    from pathlib import Path

    found = shutil.which("claude")
    if found:
        return found
    for p in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"),
              Path("/usr/local/bin/claude")):
        if p.exists():
            return str(p)
    return None


def claude_code_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in CLAUDE_CODE_HIDDEN_ENV}


def claude_code_command(system: str, model: str = "") -> list[str]:
    cmd = [claude_code_path() or "claude", "-p",
           "--output-format", "json",
           "--tools", "",                     # a model, not an agent: no shell, files, web
           "--strict-mcp-config",             # and no MCP servers
           "--no-session-persistence",
           "--system-prompt", system]         # ours replaces Claude Code's own
    if model:
        cmd += ["--model", model]
    return cmd


def _claude_code(system: str, user: str, cfg=None) -> Completion:
    import subprocess
    from pathlib import Path

    if claude_code_path() is None:
        raise ProviderUnavailable(
            "Claude Code is not installed (no `claude` command).\n"
            "  Install it, run `claude` once to sign in, then try again.")
    model = model_name(cfg)
    timeout = int(_setting(cfg, "resume.claude-code.timeout_seconds", DEFAULT_TIMEOUT_S))
    # Run away from this repository, so its CLAUDE.md and settings stay out.
    workdir = Path(__file__).resolve().parents[2] / "data" / "claude-code"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(claude_code_command(system, model), input=user, text=True,
                              capture_output=True, timeout=timeout, cwd=workdir,
                              env=claude_code_env())
    except subprocess.TimeoutExpired as exc:
        raise ProviderBusy(f"Claude Code timed out after {timeout}s") from exc

    try:
        body = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        body = {}
    said = str(body.get("result") or "").strip()
    if done.returncode or body.get("is_error") or not said:
        problem = (said or done.stderr or f"exit {done.returncode}").strip()[:300]
        low = problem.lower()
        if "limit" in low or "overloaded" in low or "529" in low or "rate" in low:
            raise ProviderBusy(f"Claude Code: {problem}")
        if "log in" in low or "login" in low or "authenticat" in low or "credit balance" in low:
            raise ProviderUnavailable(
                f"Claude Code is not signed in to a plan ({problem}).\n"
                f"  Run `claude` in a terminal and sign in, then try again.")
        raise ProviderUnavailable(f"Claude Code: {problem}")

    usage = body.get("usage") or {}
    log.info("Tailored with Claude via Claude Code%s (%s prompt / %s response tokens, "
             "on your plan)", f" ({model})" if model else "",
             usage.get("input_tokens"), usage.get("output_tokens"))
    return Completion(text=said, model=model or "claude (plan default)", provider="claude-code",
                      usage=None)


# -- remote -----------------------------------------------------------------

def _anthropic(system: str, user: str, cfg=None, *, max_tokens: int = 16000) -> Completion:
    import anthropic

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise ProviderUnavailable(
            "ANTHROPIC_API_KEY is not set, and resume.provider is 'anthropic'.\n"
            "  Either add the key to .env, or set resume.provider: ollama in "
            "config.yaml to use a model on this machine for free."
        )
    model = model_name(cfg)          # not the config directly: RESUME_MODEL must win
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
    env_key = spec.get("env_key")
    # A self-hosted gateway authenticates by being on localhost. Demanding a
    # key there would make a working setup look broken.
    api_key = os.getenv(env_key, "").strip() if env_key else ""
    if env_key and not api_key and not spec.get("key_optional"):
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
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
            json=payload, timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ProviderBusy(f"Could not reach {base_url}: {exc}") from exc

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
    if response.status_code == 429 or response.status_code >= 500:
        raise ProviderBusy(
            f"{provider} returned {response.status_code}: {response.text[:300]}")
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
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("No JSON object found in the model response")
    end = cleaned.rfind("}")
    whole = cleaned[start : end + 1] if end > start else cleaned[start:]
    try:
        return json.loads(whole)
    except json.JSONDecodeError as exc:
        # A reply cut off by the context limit, or broken by one bad escape:
        # keep everything up to the last complete entry before the damage.
        # Whatever survives still goes through the fabrication guard.
        # The whole reply first (a cut-off one, or a '}' inside a string that
        # fooled the outermost-brace guess), then only what precedes the error.
        repaired = repair_json(cleaned[start:]) or repair_json(whole[: exc.pos] if exc.pos else whole)
        if repaired is None:
            raise
        log.warning("Model reply was not valid JSON (%s) — used the complete part", exc.msg)
        return repaired


def repair_json(text: str) -> dict | None:
    """The longest valid prefix of a JSON object, closed properly. None if none.

    Walks the text tracking strings and brackets, remembers the last point
    where an element ended (a comma or a closing bracket, outside a string),
    cuts there and appends the closers still open.
    """
    stack, in_str, escaped, safe = [], False, False, None
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            if not stack:
                safe = (i + 1, [])
                break
            safe = (i + 1, list(stack))
        elif ch == ",":
            safe = (i, list(stack))
    if safe is None:
        return None
    cut, still_open = safe
    body = text[:cut].rstrip().rstrip(",")
    body += "".join("}" if c == "{" else "]" for c in reversed(still_open))
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
