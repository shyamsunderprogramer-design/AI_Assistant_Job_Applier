"""Which AI writers the app may use (A1.1.1, Nov 2026): only ones verified to cost nothing per use, unless the
person names another.

Verified, and so used without asking -- as the chosen writer, or as a fallback when it is busy:
  * Ollama with a model on this Mac: the host on this machine and not a cloud model. Ollama bills its cloud
    models per token ("Pay per token for every cloud model", ollama.com/pricing, read 10 Nov 2026), so a
    ":cloud" model is not free, whatever the provider table says.
  * Claude Code signed in with a Claude plan: `claude auth status` says claude.ai, first party. The API-key
    variables are hidden from it (llm.CLAUDE_CODE_HIDDEN_ENV), so it cannot bill an API account.

Not verified, so off unless the person names it in `resume.allow_writers` (config.yaml):
  * "chatgpt"  -- ChatGPT through Codex: a plan can draw on extra credits bought for it.
  * "gemini"   -- Gemini CLI: its billing could not be checked against Google's own terms.
  * "ollama-cloud", "ollama-remote" -- Ollama's cloud models; an Ollama on another machine.
  * "omniroute", "openrouter" -- gateways that route to services which may bill.
  * "claude-code" -- only to vouch for a Claude Code that is signed in some other way.
A writer that bills per use (the Anthropic, OpenAI, OpenRouter or xAI API) is never a fallback, and as the
chosen writer it writes only inside a spending limit the person said yes to for that run (ml/resume/cost.budget).

Nothing in config.yaml is changed or removed by this: `fallback_models`, `fallback_to_plans` and the provider
stay as the person set them, and are only filtered here -- naming a writer in `allow_writers` turns it back on.
When no approved writer is available, nothing is written: WriterNotApproved says why.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from urllib.parse import urlparse

from ml.resume.llm import DEFAULT_OLLAMA_HOST, PROVIDERS, ProviderUnavailable, _setting

UNVERIFIED = {
    "chatgpt": "ChatGPT through Codex can draw on extra credits bought for your plan",
    "gemini": "Gemini CLI's billing could not be verified",
    "omniroute": "OmniRoute routes to whichever services it is set up with, which may bill",
}
STATUS_TTL_S = 60          # read again each minute: a sign-in changed meanwhile is seen soon


class WriterNotApproved(ProviderUnavailable):
    """No writer verified to cost nothing per use (or named by the person) is available for this call."""


def _allowed(cfg) -> set[str]:
    named = _setting(cfg, "resume.allow_writers", []) or []
    if isinstance(named, str):                       # "chatgpt, gemini" written as one line
        named = named.replace(";", ",").split(",")
    return {str(n).strip().lower() for n in named if str(n).strip()}


def is_cloud_model(model: str) -> bool:
    """An Ollama model that runs on Ollama's servers, billed per token: "glm-5.3-flash:cloud", "x:120b-cloud"."""
    name = str(model or "").strip().lower()
    tag = name.rsplit("/", 1)[-1].rsplit(":", 1)[1] if ":" in name.rsplit("/", 1)[-1] else ""
    # "x:cloud", "x:120b-cloud", "x:cloud-q4", "registry:5000/x:cloud", "x-cloud": anything naming the cloud
    return "cloud" in tag.split("-") or tag.startswith("cloud") or name.endswith("-cloud")


def _local_host(url: str) -> bool:
    host = (urlparse(url if "://" in url else f"http://{url}").hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


_PLAN = {"at": -1e9, "status": {}}
_PLAN_LOCK = threading.Lock()


def _claude_plan() -> dict:
    """`claude auth status`, read at most once a minute. {} when it cannot be read: never taken for a plan."""
    from ml.resume import llm, plans
    with _PLAN_LOCK:
        if time.monotonic() - _PLAN["at"] < STATUS_TTL_S:
            return dict(_PLAN["status"])
        status = {}
        path = llm.claude_code_path()
        if path:
            try:
                done = subprocess.run([path, "auth", "status"], capture_output=True, text=True, timeout=20,
                                      env={k: v for k, v in plans._env().items()
                                           if k not in llm.CLAUDE_CODE_HIDDEN_ENV})
                body = json.loads(done.stdout or "{}")
                status = body if isinstance(body, dict) else {}
            except (OSError, ValueError, subprocess.SubprocessError):
                status = {}
        _PLAN.update(at=time.monotonic(), status=status)
        return dict(status)


def verdict(provider: str, cfg=None, *, model: str | None = None) -> tuple[bool, str]:
    """(may this writer be used now, why -- in the person's words)."""
    from ml.resume import cost, llm
    provider = str(provider or "").strip().lower()
    allowed = _allowed(cfg)
    spec = PROVIDERS.get(provider)
    if spec is None:
        return False, (f"{provider!r} is not a writer this app knows -- use one of: {', '.join(sorted(PROVIDERS))} "
                       "(ollama is local and free)")
    if provider == "ollama":
        host = str(_setting(cfg, "resume.ollama.host", DEFAULT_OLLAMA_HOST))
        model = model if model is not None else llm.model_name(cfg, provider="ollama")
        if not _local_host(host) and "ollama-remote" not in allowed:
            return False, f"Ollama at {urlparse(host).hostname or host} is not on this Mac"
        if is_cloud_model(model) and "ollama-cloud" not in allowed:
            return False, f"{model} is an Ollama cloud model, which Ollama bills per token"
        return True, "a model on this Mac" if not is_cloud_model(model) else "an Ollama cloud model you allowed"
    if provider == "claude-code":
        status = _claude_plan()
        if (status.get("loggedIn") and status.get("authMethod") == "claude.ai"
                and status.get("apiProvider", "firstParty") == "firstParty"):
            return True, "your Claude plan"
        if "claude-code" in allowed:
            return True, "Claude Code, allowed by you"
        return False, "Claude Code is not signed in with a Claude plan"
    if not spec.get("free", False):                    # bills per use
        if cost.active_budgets():
            return True, "billed per use, inside the spending limit you said yes to"
        return False, f"{spec.get('label', provider)} bills per use and no spending limit is open for this work"
    if provider in allowed:
        return True, f"{spec.get('label', provider)}, allowed by you"
    return False, UNVERIFIED.get(provider, f"{spec.get('label', provider)} is not verified to cost nothing per use")


def free_writer(provider: str, cfg=None) -> bool:
    """Used with no bill per use: a verified writer, or an unbilled one the person named. A billed one never."""
    spec = PROVIDERS.get(str(provider or "").lower(), {})
    if not spec.get("free", False):
        return False
    return verdict(provider, cfg)[0]


def primary_problem(cfg=None) -> str | None:
    """Why the writer chosen in Settings may not write at all, or None. A writer that bills is not judged here:
    the person's yes and a spending limit decide it for each run."""
    from ml.resume import llm
    provider = llm.provider_name(cfg)
    spec = PROVIDERS.get(provider)
    if spec is not None and not spec.get("free", False):
        return None
    ok, why = verdict(provider, cfg)
    if ok:
        return None
    return (f"The writer chosen in Settings cannot be used: {why}. Choose Claude (your plan) or a model on this "
            "Mac in Settings, or name it in resume.allow_writers if you accept that.")


def require(provider: str, cfg=None, *, model: str | None = None) -> None:
    ok, why = verdict(provider, cfg, model=model)
    if not ok:
        raise WriterNotApproved(f"Not written: {why}. Nothing was sent to it.")
