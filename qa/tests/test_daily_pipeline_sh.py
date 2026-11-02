"""devops/daily_pipeline.sh itself, run against a throwaway project with stand-ins for python,
pmset and caffeinate: the 6 AM switch, the wait for the lid, and its lock."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "devops" / "daily_pipeline.sh"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "data").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    stub = {
        root / ".venv" / "bin" / "python": f'#!/bin/bash\necho "$*" >> "{calls}"\nexit 0\n',
        bin_dir / "caffeinate": '#!/bin/bash\nwhile [[ "$1" == -* ]]; do shift; done\nexec "$@"\n',
        # Asleep (a Power Nap wake: no graphics) for the first PMSET_ASLEEP calls, then open.
        bin_dir / "pmset": (f'#!/bin/bash\nf="{tmp_path}/pmset_calls"\nn=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 ))\n'
                            'echo $n > "$f"\nif [ "$n" -le "${PMSET_ASLEEP:-0}" ]; then\n'
                            '  echo "Current System Capabilities are: CPU Disk Network"\nelse\n'
                            '  echo "Current System Capabilities are: CPU Graphics Audio Network"\nfi\n'),
    }
    for path, text in stub.items():
        path.write_text(text)
        path.chmod(0o755)

    def run(**env):
        full = {"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin", "PIPELINE_PROJECT": str(root),
                "NOTIFY": "0", "WAKE_POLL": "0.05", "HOME": os.environ.get("HOME", "/tmp"), **env}
        done = subprocess.run(["/bin/bash", str(SCRIPT), "daily"], env=full, capture_output=True,
                              text=True, timeout=30)
        log = (root / "data" / "daily_run.log")
        return done, log.read_text() if log.exists() else "", calls.read_text() if calls.exists() else ""
    return root, run


def test_switched_off_the_6am_tick_does_nothing(project):
    root, run = project
    (root / "data" / "daily_auto_off").write_text("off")
    done, log, calls = run()
    assert done.returncode == 0 and '"skipped":"switched off"' in done.stdout
    assert "switched off on the Jobs page" in log and calls == ""
    assert not (root / "data" / "daily.lock").exists()


@pytest.mark.parametrize("manual", ["0", "1"])
def test_no_run_before_there_is_a_resume_to_search_with(project, manual):
    """A new install's first 6 AM, or Start pressed before the first upload: nothing to look for."""
    root, run = project
    python = root / ".venv" / "bin" / "python"
    python.write_text(python.read_text().replace(
        "exit 0", 'if [ "$*" = "-m backend.core.setup_state why" ]; then echo "there is no resume yet"; fi\nexit 0'))
    done, log, calls = run(MANUAL=manual)
    assert done.returncode == 0 and '"skipped":"not set up"' in done.stdout
    assert "not starting — there is no resume yet" in log
    assert "main.py daily" not in calls and not (root / "data" / "daily.lock").exists()


def test_start_from_the_page_runs_whatever_the_switch_says(project):
    root, run = project
    (root / "data" / "daily_auto_off").write_text("off")
    done, log, calls = run(MANUAL="1", PMSET_ASLEEP="99")       # someone pressed Start: no waiting
    assert done.returncode == 0
    assert "started from the Jobs page" in log and "waiting for the Mac" not in log
    assert "main.py daily" in calls
    assert "main.py prepare-apply" not in calls                  # resumes wait for Auto-apply / Prepare now
    assert "--cloud" not in calls                                # Start now reads the boards here
    assert "devops.cloud_results push-boards" in calls           # the cloud's list kept up to date
    assert not (root / "data" / "daily.lock").exists() and not (root / "data" / "daily.pid").exists()


def test_a_scheduled_run_waits_for_the_lid_then_runs(project):
    root, run = project
    done, log, calls = run(PMSET_ASLEEP="3")
    assert done.returncode == 0
    assert log.index("waiting for the Mac to be opened") < log.index("the Mac is open — starting")
    assert "main.py daily --cloud" in calls                      # a scheduled morning takes GitHub's fetch


def test_a_mac_that_was_off_says_so_and_runs_as_the_schedule_would(project):
    root, run = project
    done, log, calls = run(CATCH_UP="1")
    assert done.returncode == 0 and "the 6 AM start had not run" in log
    assert "main.py daily --cloud" in calls


def test_a_live_run_keeps_its_lock_and_a_dead_ones_lock_is_cleared(project):
    root, run = project
    lock, pidfile = root / "data" / "daily.lock", root / "data" / "daily.pid"
    lock.mkdir()
    # A live run: a process whose command line names the script, like the real one.
    live = subprocess.Popen(["/bin/bash", "-c", 'exec -a "daily_pipeline.sh daily" sleep 20'])
    try:
        pidfile.write_text(str(live.pid))
        done, log, calls = run(MANUAL="1")
        assert '"skipped":"already running"' in done.stdout and "main.py" not in calls
    finally:
        live.kill()
        live.wait()
    # Its run is gone (killed, or stopped from the page): the next one clears the lock and runs.
    done, log, calls = run(MANUAL="1")
    assert "clearing a lock its run left behind" in log and "main.py daily" in calls
    assert not lock.exists()


# -- the night's postings (data/cloud_topup.json) ----------------------------------------------------

def top_up_stub(root, wait_rc=0, merge_rc=0, merge_sleep=0):
    """python as the top-up sees it. The wait records which locks exist and what its parent process
    is called (the web app's Start looks for a process ending in "daily_pipeline.sh daily"); the
    merge records whether it holds the morning lock."""
    python = root / ".venv" / "bin" / "python"
    calls = Path(python.read_text().split('>> "')[1].split('"')[0])
    data = root / "data"
    python.write_text(f"""#!/bin/bash
echo "$*" >> "{calls}"
if [ "$*" = "-u -m devops.cloud_results wait-top-up" ]; then
  [ -d "{data}/daily.lock" ] && echo "wait: morning lock held" >> "{calls}"
  [ -d "{data}/topup.lock" ] && echo "wait: own lock held" >> "{calls}"
  echo "wait: parent $(ps -o args= -p $PPID)" >> "{calls}"
  exit {wait_rc}
fi
if [ "$*" = "-u main.py daily --top-up" ]; then
  [ -d "{data}/daily.lock" ] && echo "merge: morning lock held" >> "{calls}"
  sleep {merge_sleep}
  rm -f "{data}/cloud_topup.json"
  exit {merge_rc}
fi
exit 0
""")


def asked(root):
    (root / "data" / "cloud_topup.json").write_text('{"after": 41, "at": 0}')


def test_nothing_asked_no_top_up(project):
    root, run = project
    top_up_stub(root)
    done, log, calls = run()
    assert "wait-top-up" not in calls and "--top-up" not in calls and "top-up" not in log


def test_the_wait_holds_no_morning_lock_and_the_merge_holds_it(project):
    """The morning run lets its lock go and becomes the top-up: during the wait, nothing the web
    app's Start or its fetches look at is held; the merge alone takes the morning lock."""
    root, run = project
    top_up_stub(root)
    asked(root)
    done, log, calls = run()
    assert done.returncode == 0 and '"task":"topup","exit":0' in done.stdout
    lines = calls.splitlines()
    assert lines.index("-u main.py daily --cloud") < lines.index("-u -m devops.cloud_results wait-top-up") \
        < lines.index("-u main.py daily --top-up")
    assert "wait: morning lock held" not in calls and "wait: own lock held" in calls
    parent = next(line for line in lines if line.startswith("wait: parent"))
    assert parent.endswith("daily_pipeline.sh topup") and not parent.endswith(" daily")
    assert "merge: morning lock held" in calls
    assert log.index("=== daily finished") < log.index("morning lock released") \
        < log.index("waiting for the night's GitHub fetch") < log.index("=== top-up starting") \
        < log.index("=== top-up finished, exit 0")
    for left in ("daily.lock", "daily.pid", "topup.lock", "topup.pid", "cloud_topup.json"):
        assert not (root / "data" / left).exists(), left


def test_nothing_to_add_takes_no_lock_and_merges_nothing(project):
    root, run = project
    top_up_stub(root, wait_rc=1)
    asked(root)
    done, log, calls = run()
    assert done.returncode == 0 and '"added":false' in done.stdout
    assert "--top-up" not in calls and "top-up: nothing to add" in log
    assert not (root / "data" / "daily.lock").exists() and not (root / "data" / "topup.lock").exists()


def live_holder(root, task="daily"):
    """A live run holding `task`'s lock, its command line naming the script, like the real one."""
    (root / "data" / f"{task}.lock").mkdir()
    holder = subprocess.Popen(["/bin/bash", "-c", f'exec -a "daily_pipeline.sh {task}" sleep 20'])
    (root / "data" / f"{task}.pid").write_text(str(holder.pid))
    return holder


def test_a_run_holding_the_morning_lock_is_waited_out_then_the_merge_runs(project):
    """Start pressed during the wait: its run holds the morning lock when the fetch is ready, and
    the merge waits for it instead of writing beside it."""
    root, run = project
    top_up_stub(root)
    asked(root)
    holder = live_holder(root)
    # The scheduled morning run itself must not stop at the holder's lock: start it as a top-up.
    import threading
    threading.Timer(1.5, holder.kill).start()
    try:
        done, log, calls = run_topup(root, TOPUP_LOCK_POLL="0.2", TOPUP_LOCK_TRIES="50")
    finally:
        holder.kill()
        holder.wait()
    assert done.returncode == 0 and "another run holds the morning lock" in log
    assert log.count("another run holds the morning lock") == 1 and "already running" not in log
    assert "merge: morning lock held" in calls and "-u main.py daily --top-up" in calls


def test_a_lock_that_stays_busy_defers_the_merge_and_keeps_the_ask(project):
    root, run = project
    top_up_stub(root)
    asked(root)
    holder = live_holder(root)
    try:
        done, log, calls = run_topup(root, TOPUP_LOCK_POLL="0.05", TOPUP_LOCK_TRIES="3")
    finally:
        holder.kill()
        holder.wait()
    assert done.returncode == 0 and '"deferred":true' in done.stdout
    assert "--top-up" not in calls and "kept for the next run" in log
    assert (root / "data" / "cloud_topup.json").exists()                 # the ask is not lost
    assert not (root / "data" / "topup.lock").exists()
    assert (root / "data" / "daily.lock").is_dir()                         # the holder's lock untouched


def test_one_wait_at_a_time(project):
    """A morning run (Start) that ends while a top-up waits does not start a second wait."""
    root, run = project
    top_up_stub(root)
    asked(root)
    holder = live_holder(root, "topup")
    try:
        done, log, calls = run(MANUAL="1")
    finally:
        holder.kill()
        holder.wait()
    assert "-u main.py daily" in calls and "wait-top-up" not in calls
    assert "topup already running" in log and not (root / "data" / "daily.lock").exists()
    assert (root / "data" / "cloud_topup.json").exists()


def test_a_failed_merge_is_reported_and_lets_every_lock_go(project):
    root, run = project
    top_up_stub(root, merge_rc=1)
    asked(root)
    done, log, calls = run()
    assert done.returncode == 1 and '"ok":false,"task":"topup","exit":1' in done.stdout
    assert "=== top-up finished, exit 1" in log
    for left in ("daily.lock", "daily.pid", "topup.lock", "topup.pid"):
        assert not (root / "data" / left).exists(), left


def test_the_merge_runs_even_with_the_6am_start_switched_off(project):
    """Switching off the 6 AM start stops the next morning, not the end of this one."""
    root, run = project
    top_up_stub(root)
    asked(root)
    (root / "data" / "daily_auto_off").write_text("off")
    done, log, calls = run(MANUAL="1")
    assert "merge: morning lock held" in calls


def run_topup(root, **env):
    """devops/daily_pipeline.sh topup, as a morning run becomes it when it ends."""
    calls = Path((root / ".venv" / "bin" / "python").read_text().split('>> "')[1].split('"')[0])
    bin_dir = calls.parent / "bin"
    full = {"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin", "PIPELINE_PROJECT": str(root),
            "NOTIFY": "0", "WAKE_POLL": "0.05", "HOME": os.environ.get("HOME", "/tmp"), **env}
    done = subprocess.run(["/bin/bash", str(SCRIPT), "topup"], env=full, capture_output=True,
                          text=True, timeout=30)
    log = root / "data" / "daily_run.log"
    return done, log.read_text() if log.exists() else "", calls.read_text() if calls.exists() else ""
