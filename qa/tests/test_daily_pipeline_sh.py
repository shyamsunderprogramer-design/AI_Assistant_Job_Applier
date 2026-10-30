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
