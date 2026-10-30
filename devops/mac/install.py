"""Install, update or remove Job Applier on this Mac: for one person, in their own Library folder.

The Mac app (Job Applier.app, made by devops/mac/build_app.sh) runs this through install.sh the first
time it is opened, and again when it is a newer copy than the one installed. Nobody needs a
Terminal. In order:

  1. (install.sh) a Python for this installer, from uv, both pinned and checked;
  2. the app's files are copied into <home>/app. Everything the person made there -- the resume,
     their details and answers, jobs.db, .env -- is left alone: only files the previous version
     installed are ever replaced or removed;
  3. the packages the app needs, exactly as listed in devops/mac/requirements-<chip>.lock, each
     file checked against its sha256;
  4. the browser applications are filled in with (Playwright's Chromium);
  5. the job database, with the list of job boards to read (backend/config/boards.csv);
  6. two launchd agents: the web app, started at login and kept running, and the 6 AM morning run;
     then a wait for the web app to answer.

Every step is reported in <home>/status.txt, which the app's progress window reads, and logged in
<home>/install.log. Uses nothing outside Python's standard library: it runs before any package is.

    python install.py --payload DIR --version V [--home DIR] [--label PREFIX] [--uv PATH]
    python install.py --uninstall [--home DIR] [--label PREFIX]

<home> is ~/Library/Application Support/Job Applier unless --home or JOB_APPLIER_HOME says
otherwise, and the agents are <label>.webapp and <label>.daily with the label com.jobapplier
unless --label or JOB_APPLIER_LABEL says otherwise -- a second copy, for testing, gets both.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_HOME = Path.home() / "Library" / "Application Support" / "Job Applier"
DEFAULT_LABEL = "com.jobapplier"
FIRST_PORT = 8770                 # the port the app has always used; the next free one if taken
LAST_PORT = 8799
STEPS = 6
MIN_MACOS = (13, 0)               # the oldest the ready-built packages support (pypdfium2)
# A launchd job gets no PATH of its own: these find Homebrew's and the chat plans' tools too.
AGENT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class Stop(RuntimeError):
    """A step that cannot go on, in words for the person, not a traceback."""


# -- reporting ------------------------------------------------------------------------------

def report(home: Path, state: str = "running", step: int = 0, text: str = "", **extra) -> None:
    """status.txt, replaced whole each time so the app never reads half of one."""
    lines = {"state": state, "step": step, "total": STEPS, "text": text, **extra}
    body = "".join(f"{key}={' '.join(str(value).split())}\n" for key, value in lines.items())
    tmp = home / "status.txt.tmp"
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, home / "status.txt")
    if text:
        say(text)


def say(line: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}", flush=True)


def run(cmd: list, cwd: Path | None = None, env: dict | None = None, timeout: int = 1800,
        what: str = "") -> str:
    """A command whose output goes to the log; a failure stops the install with `what` said."""
    say("$ " + " ".join(str(c) for c in cmd))
    done = subprocess.run([str(c) for c in cmd], cwd=cwd, env={**os.environ, **(env or {})},
                          capture_output=True, text=True, timeout=timeout)
    output = (done.stdout + done.stderr).strip()
    if output:
        print(output[-6000:], flush=True)
    if done.returncode != 0:
        raise Stop(what or f"{Path(str(cmd[0])).name} failed (exit {done.returncode})")
    return done.stdout


# -- 2. the app's files -------------------------------------------------------------------------

def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def payload_files(payload: Path) -> list[str]:
    """Every file in the payload, as relative paths with forward slashes, sorted."""
    return sorted(p.relative_to(payload).as_posix() for p in payload.rglob("*") if p.is_file()
                  and not p.is_symlink())


def sync(payload: Path, app: Path, previous: list[str]) -> list[str]:
    """Copy the payload into app/ and return what was installed. A file is written beside its
    place and renamed over it, so a script that is running keeps reading the copy it started
    with. A file the previous version installed and this one does not is removed; nothing else
    in app/ is ever touched -- that is where the person's own files live."""
    files = payload_files(payload)
    for rel in files:
        source, target = payload / rel, app / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = source.stat().st_mode & 0o777
        if target.is_file() and not target.is_symlink() and _digest(target) == _digest(source):
            if target.stat().st_mode & 0o777 != mode:
                target.chmod(mode)
            continue
        tmp = target.with_name(f".{target.name}.installing")
        shutil.copyfile(source, tmp)
        tmp.chmod(mode)
        os.replace(tmp, target)
    keep = set(files)
    for rel in previous:
        old = app / rel
        if rel not in keep and old.is_file() and not old.is_symlink():
            old.unlink()
    return files


# -- 3, 4, 5. packages, browser, database ---------------------------------------------------------

def chip() -> str:
    machine = platform.machine()
    if machine not in ("arm64", "x86_64"):
        raise Stop(f"Job Applier runs on Macs with Apple or Intel chips, not {machine}.")
    return machine


def uv_env(home: Path) -> dict:
    return {"UV_PYTHON_INSTALL_DIR": str(home / "runtime" / "python"),
            "UV_CACHE_DIR": str(home / "runtime" / "cache"), "UV_NO_CONFIG": "1",
            "UV_MANAGED_PYTHON": "1"}


def packages(app: Path, home: Path, uv: Path, done_before: str | None) -> str:
    """The app's Python environment, with exactly the packages in the lock for this chip.
    Returns the lock's fingerprint: an update with the same lock skips the reinstall."""
    lock = app / "devops" / "mac" / f"requirements-{chip()}.lock"
    mark = _digest(lock)
    python = app / ".venv" / "bin" / "python"
    if mark == done_before and python.exists():
        say("packages: unchanged since the last install")
        return mark
    if not python.exists():
        run([uv, "venv", app / ".venv", "--python", "3.12"], env=uv_env(home),
            what="Could not make the Python environment.")
    run([uv, "pip", "install", "--python", python, "--require-hashes", "--only-binary", ":all:",
         "--no-binary", "dkimpy", "-r", lock], env=uv_env(home), timeout=3600,
        what="Could not install the packages Job Applier needs. Check the internet connection "
             "and open Job Applier again: it carries on from where it stopped.")
    return mark


def browser(app: Path) -> None:
    run([app / ".venv" / "bin" / "python", "-m", "playwright", "install", "chromium"], cwd=app,
        timeout=3600, what="Could not download the browser Job Applier fills applications in. "
                           "Check the internet connection and open Job Applier again.")


def database(app: Path) -> None:
    python = app / ".venv" / "bin" / "python"
    run([python, "main.py", "init-db"], cwd=app, what="Could not create the job database.")
    run([python, "-m", "data_engineering.boards", "import"], cwd=app,
        what="Could not load the list of job boards.")


# -- 6. the port, the agents, the wait ------------------------------------------------------------

def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def read_env(path: Path) -> dict:
    values = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.lstrip().startswith("#"):
                values[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    return values


def choose_port(app: Path, installed: dict, free=port_free) -> int:
    """The port this copy keeps: the one it was given before, else the first free from 8770."""
    for known in (read_env(app / ".env").get("JOB_APPLIER_PORT"), installed.get("port")):
        if str(known or "").isdigit():
            return int(known)
    for port in range(FIRST_PORT, LAST_PORT + 1):
        if free(port):
            return port
    raise Stop(f"Every port from {FIRST_PORT} to {LAST_PORT} is in use on this Mac.")


def remember_port(app: Path, port: int) -> None:
    """In .env, which the app reads (backend/config/loader.web_port), when it is not 8770."""
    if port == FIRST_PORT and "JOB_APPLIER_PORT" not in read_env(app / ".env"):
        return
    sys.path.insert(0, str(app))
    from backend.config.envfile import write_env      # the app's own: keeps what is already there
    write_env(app / ".env", {"JOB_APPLIER_PORT": str(port)})


def agents(app: Path, label: str) -> dict[str, dict]:
    """The two launchd agents, as plists, for this copy's folder."""
    logs = app / "data"
    env = {"PATH": AGENT_PATH}
    return {
        f"{label}.webapp": {
            "Label": f"{label}.webapp",
            "ProgramArguments": [str(app / ".venv" / "bin" / "python"), "-m", "backend.api"],
            "WorkingDirectory": str(app),
            "EnvironmentVariables": env,
            "RunAtLoad": True,
            "KeepAlive": True,
            # The pages someone is waiting for. As "Background" macOS ran it at priority 4 and
            # slowed its disk reads: the Jobs page took 1.5-2.2 s to build, 0.5 s otherwise (21 Oct 2026).
            "ProcessType": "Interactive",
            "StandardOutPath": str(logs / "webapp.log"),
            "StandardErrorPath": str(logs / "webapp.log"),
        },
        f"{label}.daily": {
            "Label": f"{label}.daily",
            "ProgramArguments": ["/bin/bash", str(app / "devops" / "daily_pipeline.sh"), "daily"],
            "WorkingDirectory": str(app),
            "EnvironmentVariables": env,
            "StartCalendarInterval": [{"Hour": 6, "Minute": 0}],
            "RunAtLoad": False,
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "Nice": 5,
            "StandardOutPath": str(logs / "launchd_daily.log"),
            "StandardErrorPath": str(logs / "launchd_daily.log"),
        },
    }


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def stop_agent(name: str) -> None:
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{name}"], capture_output=True, timeout=60)


def start_agents(app: Path, label: str) -> None:
    folder = launch_agents_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (app / "data").mkdir(parents=True, exist_ok=True)
    for name, spec in agents(app, label).items():
        path = folder / f"{name}.plist"
        path.write_bytes(plistlib.dumps(spec))
        stop_agent(name)
        refused = ""
        for attempt in range(5):                     # bootout can take a moment to let go
            done = subprocess.run(["launchctl", "bootstrap", _domain(), str(path)],
                                  capture_output=True, text=True, timeout=60)
            if done.returncode == 0:
                refused = ""
                break
            refused = done.stderr.strip()[:200] or f"exit {done.returncode}"
            time.sleep(1 + attempt)
        if refused:
            raise Stop(f"macOS would not start {name}: {refused}")


def wait_for(port: int, seconds: int = 120) -> None:
    """Until the web app answers. Any answer counts -- an error page is the app's to explain, on
    the page, not a reason to call a finished install a failed one."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/welcome", timeout=5):
                return
        except urllib.error.HTTPError:
            return
        except OSError:
            pass
        time.sleep(1)
    raise Stop("Job Applier was installed but did not start. The details are in data/webapp.log.")


# -- what is running from this copy -----------------------------------------------------------

def processes_under(app: Path, ps_output: str | None = None) -> list[tuple[int, str]]:
    """Processes started from this copy's folder: its web app, a morning run, a tailoring run.
    Matched on the folder as a whole path, so a second copy beside it is not one of them."""
    if ps_output is None:
        ps_output = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True,
                                   timeout=10).stdout
    folder = str(app).rstrip("/") + "/"
    found = []
    for line in ps_output.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and folder in args and int(pid) not in (os.getpid(), os.getppid()):
            found.append((int(pid), args.strip()))
    return found


def morning_run_going(app: Path) -> bool:
    tail = f" {app / 'devops' / 'daily_pipeline.sh'} daily"
    return any(args.endswith(tail) for _, args in processes_under(app))


def end_processes(app: Path) -> None:
    for pid, _ in processes_under(app):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


# -- the whole of it -------------------------------------------------------------------------------

def read_installed(home: Path) -> dict:
    try:
        return json.loads((home / "installed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_installed(home: Path, record: dict) -> None:
    tmp = home / "installed.json.tmp"
    tmp.write_text(json.dumps(record, indent=1), encoding="utf-8")
    os.replace(tmp, home / "installed.json")
    # The same three facts, plainly, for the Mac app's AppleScript to read without a JSON parser.
    (home / "installed.txt").write_text(
        f"version={record['version']}\nport={record['port']}\nlabel={record['label']}\n", encoding="utf-8")


def macos_version() -> tuple[int, int]:
    parts = (platform.mac_ver()[0] or "0.0").split(".")
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0


def install(payload: Path, home: Path, label: str, version: str, uv: Path) -> int:
    app = home / "app"
    installed = read_installed(home)
    updating = bool(installed)
    if macos_version() < MIN_MACOS:
        raise Stop(f"Job Applier needs macOS {MIN_MACOS[0]} (Ventura) or later; this Mac has "
                   f"{platform.mac_ver()[0]}.")
    if updating and morning_run_going(app):
        raise Stop("Job Applier is finding jobs right now. Open it again to update once that has "
                   "finished; the Jobs page shows how far it has got.")

    report(home, step=2, text="Copying Job Applier's files…")
    if updating:
        stop_agent(f"{installed.get('label', label)}.webapp")    # no page served from half-new code
    files = sync(payload, app, installed.get("files", []))

    report(home, step=3, text="Installing the parts it needs (a few minutes)…")
    lock = packages(app, home, uv, installed.get("lock"))

    report(home, step=4, text="Getting the browser it fills applications in (the biggest download)…")
    browser(app)

    report(home, step=5, text="Setting up your job database…")
    database(app)

    report(home, step=6, text="Starting Job Applier…")
    port = choose_port(app, installed)
    remember_port(app, port)
    if installed.get("label") and installed["label"] != label:
        remove_agents(installed["label"])
    start_agents(app, label)
    wait_for(port)

    write_installed(home, {"version": version, "port": port, "label": label, "files": files,
                           "lock": lock, "installed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    # uv's download cache is ~400 MB nobody needs once the packages are in place (installing
    # copies them out of it), and an update with an unchanged lock never reads it.
    subprocess.run([str(uv), "cache", "clean"], env={**os.environ, **uv_env(home)}, capture_output=True,
                   timeout=300)
    page = "/" if updating else "/welcome"
    report(home, state="done", step=STEPS, text="Job Applier is ready.",
           url=f"http://localhost:{port}{page}")
    return 0


def remove_agents(label: str) -> None:
    for name in (f"{label}.webapp", f"{label}.daily"):
        stop_agent(name)
        (launch_agents_dir() / f"{name}.plist").unlink(missing_ok=True)


def uninstall(home: Path, label: str) -> int:
    """Stop and remove the agents and anything running from this copy. The folder itself is
    moved to the Trash by the Mac app afterwards, so the person can still take it back."""
    installed = read_installed(home)
    remove_agents(installed.get("label") or label)
    end_processes(home / "app")
    say("removed the agents and stopped what was running")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--payload", type=Path)
    ap.add_argument("--version", default="unknown")
    ap.add_argument("--home", type=Path, default=Path(os.environ.get("JOB_APPLIER_HOME") or DEFAULT_HOME))
    ap.add_argument("--label", default=os.environ.get("JOB_APPLIER_LABEL") or DEFAULT_LABEL)
    ap.add_argument("--uv", type=Path)
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args(argv)
    home = args.home.expanduser()
    home.mkdir(parents=True, exist_ok=True)
    if args.uninstall:
        return uninstall(home, args.label)
    # Its own process group, so Stop in the app's window can end this and everything it started.
    try:
        os.setpgrp()
    except OSError:
        pass
    (home / "install.pgid").write_text(str(os.getpgrp()))
    try:
        if not args.payload or not args.uv:
            raise Stop("install.py needs --payload and --uv (it is run by install.sh).")
        return install(args.payload.resolve(), home.resolve(), args.label, args.version, args.uv)
    except Stop as exc:
        report(home, state="failed", error=str(exc))
        say(f"FAILED: {exc}")
        return 1
    except Exception as exc:                          # the log keeps the traceback; the window, words
        import traceback
        traceback.print_exc()
        report(home, state="failed", error=f"Something unexpected went wrong ({type(exc).__name__}). "
                                           "The details are in install.log.")
        return 1
    finally:
        (home / "install.pgid").unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
