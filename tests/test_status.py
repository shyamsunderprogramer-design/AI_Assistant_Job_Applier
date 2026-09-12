"""Progress-view tests — no processes started, no network.

The view's job is to be trusted at a glance while long work runs unattended.
Both failures here were silent and both said the comfortable thing: a finished
run reported as stopped short, and a finished run reported as still going.
"""

import status


def test_completed_scan_reads_as_finished(tmp_path):
    log = tmp_path / "mailscan.log"
    log.write_text(
        "  17200/17248 messages — 3300 names, 0 boards so far\n"
        "\nWrote 3302 names to data/mailbox_names.txt\n",
        encoding="utf-8",
    )
    # Progress lines land on multiples of 25, so the counter stops short of the
    # total even on a clean finish.
    assert status._tail_match(log, status.MAIL_DONE) == "3302"


def test_unfinished_scan_has_no_completion_marker(tmp_path):
    log = tmp_path / "mailscan.log"
    log.write_text("  8000/17248 messages — 1200 names, 0 boards so far\n", encoding="utf-8")

    assert status._tail_match(log, status.MAIL_DONE) is None


def test_completed_discovery_reads_as_finished(tmp_path):
    log = tmp_path / "discovery.log"
    log.write_text(
        "  ... 24750/24784 names, 1014 found\n"
        "\n1016 found from 24784 names; 40074 probes sent, 32800 from cache\n",
        encoding="utf-8",
    )
    assert status._tail_match(log, status.DISCOVERY_DONE) == "1016"


def test_discovery_progress_is_counted_in_names(tmp_path):
    log = tmp_path / "discovery.log"
    log.write_text("  ... 10950/24784 names, 555 found\n", encoding="utf-8")

    done, total, _ = status._tail_progress(log, status.DISCOVERY_LINE)
    assert (done, total) == (10950, 24784)


def fake_ps(monkeypatch, listing: str):
    import subprocess

    class Result:
        stdout = listing

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Result())


def test_a_shell_mentioning_a_job_is_not_the_job(monkeypatch):
    # `sh -c "<script>"` carries the whole script on its command line, so a
    # script that merely names a job used to look exactly like the job.
    fake_ps(monkeypatch,
            "  501 /bin/zsh -c python main.py discover --names x.txt\n"
            "  502 /bin/sh -c echo main.py discover\n")

    assert status._pid_of("discover") is None


def test_a_real_job_process_is_found(monkeypatch):
    fake_ps(monkeypatch,
            "  777 /path/.venv/bin/python main.py discover --names x.txt\n")

    assert status._pid_of("discover") == 777
