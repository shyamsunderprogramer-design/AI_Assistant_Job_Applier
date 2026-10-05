"""Write with the chat plan the person already pays for: Claude, ChatGPT or Gemini.

Each company ships an official command-line tool that signs in with that
plan, and runs one prompt at a time:

  Claude    Claude Code   `claude -p`       (ml/resume/llm.py, provider claude-code)
  ChatGPT   Codex CLI     `codex exec`      "Sign in with ChatGPT"
  Gemini    Gemini CLI    `gemini -p`       "Sign in with Google"

No API key and no credit: each resume uses some of that plan's allowance.
Signing in always happens on the company's own page, opened by its own tool
in a Terminal window -- this app never sees or stores the password. The
Settings page shows who is signed in and offers Sign in, Sign out, Install.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

log = logging.getLogger(__name__)

TIMEOUT_S = 360
WORKDIR = PROJECT_ROOT / "data" / "plan-cli"

# What each tool is called, and the commands a person would type.
TOOLS = {
    "claude": {
        "label": "Claude", "provider": "claude-code", "binary": "claude",
        "signin": "claude auth login", "signout": "claude auth logout",
        "install": "curl -fsSL https://claude.ai/install.sh | bash",
        "plan": "a Claude Pro or Max plan",
    },
    "chatgpt": {
        "label": "ChatGPT", "provider": "chatgpt", "binary": "codex",
        "signin": "codex login", "signout": "codex logout",
        "install": "npm install -g @openai/codex",
        "plan": "a ChatGPT Plus, Pro or Business plan",
    },
    "gemini": {
        "label": "Gemini", "provider": "gemini", "binary": "gemini",
        # The first run asks how to sign in: choose "Login with Google".
        "signin": "gemini", "signout": "rm -f ~/.gemini/oauth_creds.json",
        "install": "npm install -g @google/gemini-cli",
        "plan": "any Google account (free daily allowance) or Gemini Advanced",
    },
}


class PlanUnavailable(RuntimeError):
    """The tool is missing or not signed in, with what to do about it."""


def tool_path(binary: str) -> str | None:
    found = shutil.which(binary)
    if found:
        return found
    for folder in (Path.home() / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
                   Path.home() / ".npm-global/bin"):
        if (folder / binary).exists():
            return str(folder / binary)
    return None


def _env() -> dict[str, str]:
    # The app has .env loaded; an API key there would bill per call instead of the plan.
    hidden = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    env = {k: v for k, v in os.environ.items() if k not in hidden}
    # Codex and the Gemini CLI run on Node. The web app runs as a background
    # service whose PATH is /usr/bin:/bin, so `codex` started and then failed
    # with "env: node: No such file or directory" -- read as "not signed in".
    extra = [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
             str(Path.home() / ".npm-global/bin")]
    env["PATH"] = os.pathsep.join(dict.fromkeys(extra + env.get("PATH", "/usr/bin:/bin").split(os.pathsep)))
    return env


# -- who is signed in -------------------------------------------------------------

def status(name: str) -> dict:
    """{installed, signed_in, who} for one tool; never raises."""
    tool = TOOLS[name]
    path = tool_path(tool["binary"])
    out = {"name": name, "label": tool["label"], "plan": tool["plan"], "installed": bool(path),
           "signed_in": False, "who": ""}
    if not path:
        return out
    try:
        if name == "claude":
            done = subprocess.run([path, "auth", "status"], capture_output=True, text=True,
                                  timeout=20, env=_env())
            body = json.loads(done.stdout or "{}")
            out["signed_in"] = bool(body.get("loggedIn"))
            out["who"] = body.get("email") or ""
        elif name == "chatgpt":
            done = subprocess.run([path, "login", "status"], capture_output=True, text=True,
                                  timeout=20, env=_env())
            said = (done.stdout + done.stderr).strip()
            out["signed_in"] = done.returncode == 0 and "not logged in" not in said.lower()
            out["who"] = "with ChatGPT" if "chatgpt" in said.lower() else said[:60]
        elif name == "gemini":
            creds = Path.home() / ".gemini" / "oauth_creds.json"
            out["signed_in"] = creds.exists()
            out["who"] = "with Google" if creds.exists() else ""
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return out


def open_in_terminal(name: str, action: str) -> str:
    """Run the tool's own sign-in, sign-out or install in a Terminal window the person sees."""
    tool = TOOLS[name]
    command = tool[action]
    banner = {"signin": f"Signing in to {tool['label']} — follow the steps here and in your browser."
                        + (" Choose 'Login with Google'." if name == "gemini" else ""),
              "signout": f"Signing out of {tool['label']}.",
              "install": f"Installing {tool['label']}'s official command-line tool."}[action]
    script = f'echo "{banner}"; echo; {command}; echo; echo "Done — you can close this window and press Check on the Settings page."'
    escaped = script.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "{escaped}"',
                    "-e", 'tell application "Terminal" to activate'],
                   capture_output=True, timeout=30)
    return command


# -- writing ------------------------------------------------------------------------

def _run(cmd: list[str], prompt: str, label: str) -> subprocess.CompletedProcess:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    try:
        return subprocess.run(cmd, input=prompt, text=True, capture_output=True,
                              timeout=TIMEOUT_S, cwd=WORKDIR, env=_env())
    except subprocess.TimeoutExpired as exc:
        from ml.resume.llm import ProviderBusy
        raise ProviderBusy(f"{label} timed out after {TIMEOUT_S}s") from exc


def _problem(label: str, signin: str, done: subprocess.CompletedProcess):
    from ml.resume.llm import ProviderBusy, ProviderUnavailable
    said = (done.stderr or done.stdout or f"exit {done.returncode}").strip()[-300:]
    low = said.lower()
    if any(w in low for w in ("rate limit", "quota", "usage limit", "overloaded", "429", "try again")):
        return ProviderBusy(f"{label}: {said}")
    if any(w in low for w in ("login", "log in", "sign in", "authenticat", "unauthorized", "401")):
        return ProviderUnavailable(f"{label} is not signed in ({said}).\n"
                                   f"  Press Sign in on the Settings page, or run `{signin}`.")
    return ProviderUnavailable(f"{label}: {said}")


def chatgpt(system: str, user: str, model: str = ""):
    """One prompt through Codex, signed in with ChatGPT."""
    from ml.resume.llm import Completion, ProviderUnavailable
    path = tool_path("codex")
    if not path:
        raise ProviderUnavailable("ChatGPT needs OpenAI's Codex CLI. Press Install on the Settings page.")
    with tempfile.TemporaryDirectory() as tmp:
        answer = Path(tmp) / "answer.txt"
        cmd = [path, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
               "--color", "never", "--output-last-message", str(answer)]
        if model:
            cmd += ["--model", model]
        done = _run(cmd + ["-"], f"{system}\n\n---\n\n{user}", "ChatGPT")
        said = answer.read_text().strip() if answer.exists() else ""
    if done.returncode or not said:
        raise _problem("ChatGPT", TOOLS["chatgpt"]["signin"], done)
    log.info("Tailored with ChatGPT via Codex%s (on your plan)", f" ({model})" if model else "")
    return Completion(text=said, model=model or "chatgpt (plan default)", provider="chatgpt", usage=None)


def gemini(system: str, user: str, model: str = ""):
    """One prompt through the Gemini CLI, signed in with Google."""
    from ml.resume.llm import Completion, ProviderUnavailable
    path = tool_path("gemini")
    if not path:
        raise ProviderUnavailable("Gemini needs Google's Gemini CLI. Press Install on the Settings page.")
    cmd = [path, "-p", system]          # the posting and resume come in on stdin
    if model:
        cmd += ["-m", model]
    done = _run(cmd, user, "Gemini")
    said = (done.stdout or "").strip()
    if done.returncode or not said:
        raise _problem("Gemini", TOOLS["gemini"]["signin"], done)
    log.info("Tailored with Gemini via Gemini CLI%s (on your Google account)", f" ({model})" if model else "")
    return Completion(text=said, model=model or "gemini (default)", provider="gemini", usage=None)
