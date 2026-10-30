"""Saves at the same moment, from several processes (29 Oct 2026): the answer bank, the written-answers
library, and essay drafts beside the prepared applications' answers. Each read the whole file and wrote it
back, and the bank and the drafts did it outside any lock -- one of two saves could undo the other, and a
save that read the bank mid-write could find it empty and write back only its own answer. Every save must
be there at the end, and a reader must never see half a file. Throwaway files only: every child process is
pointed at this test's own folder before it saves anything."""

import json
import subprocess
import sys
import threading

import yaml

from backend.apply import answers, prepare, written
from backend.config.loader import PROJECT_ROOT

WRITERS = 4


def children(tmp_path, body: str, count: int = WRITERS) -> None:
    """`count` processes at once, each running `body` with its number as `me`, all stores in tmp_path."""
    head = f"""
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from pathlib import Path
from backend.apply import answers, general, prepare, written
answers.BANK_PATH = Path({str(tmp_path / 'answers.yaml')!r})
written.PATH = Path({str(tmp_path / 'written_answers.json')!r})
general.STORE = Path({str(tmp_path / 'general_answers.yaml')!r})
prepare.PREPARED = Path({str(tmp_path / 'prepared.json')!r})
me = int(sys.argv[1])
"""
    procs = [subprocess.Popen([sys.executable, "-c", head + body, str(n)], stderr=subprocess.PIPE, text=True)
             for n in range(count)]
    for p in procs:
        assert p.wait(timeout=120) == 0, p.stderr.read()


def reading(path, parse, errors, stop):
    while not stop.is_set():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            parse(text)
        except Exception as exc:                  # half a file
            errors.append(exc)


def test_answers_learned_by_several_processes_at_once_are_all_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(answers, "BANK_PATH", tmp_path / "answers.yaml")
    assert answers.learnable("How many years have you used tool 0-0?", "3")
    errors, stop = [], threading.Event()
    reader = threading.Thread(target=reading, args=(tmp_path / "answers.yaml", yaml.safe_load, errors, stop))
    reader.start()
    try:
        children(tmp_path, """
for n in range(20):
    answers.remember({f"How many years have you used tool {me}-{n}?": str(n % 9 + 1)})
""")
    finally:
        stop.set()
        reader.join()
    assert len(answers.load()) == WRITERS * 20 and not errors


def test_written_answers_kept_by_several_processes_at_once_are_all_kept(tmp_path):
    errors, stop = [], threading.Event()
    reader = threading.Thread(target=reading, args=(written.PATH, json.loads, errors, stop))
    reader.start()
    try:
        children(tmp_path, """
for n in range(15):
    written.keep(f"Describe the largest system you have run, number {me}-{n}", f"Answer {me}-{n}: a fleet of servers.")
""")
    finally:
        stop.set()
        reader.join()
    assert len(written.load()) == WRITERS * 15 and not errors


def test_essay_drafts_and_answers_saved_at_once_are_both_kept(tmp_path):
    from types import SimpleNamespace
    for job_id in (1, 2, 3):
        prepare.keep_questions(SimpleNamespace(id=job_id, company="Example", title="Engineer", source="greenhouse",
                                               ats_match_score=0.9), [])
    errors, stop = [], threading.Event()
    reader = threading.Thread(target=reading, args=(prepare.PREPARED, json.loads, errors, stop))
    reader.start()
    try:
        children(tmp_path, """
for n in range(30):
    if me % 2:
        prepare.save_answers(1 + me % 2, {f"Answer {me}-{n}": f"value {n}"})
    else:
        prepare.keep_drafts(3, {f"Why us {me}-{n}?": {"text": f"draft {n}", "made": "2026-10-29T00:00:00+00:00"}})
""")
    finally:
        stop.set()
        reader.join()
    saved = prepare.load()
    answered = {k for job in ("1", "2") for k in saved[job]["answers"]}
    assert len(answered) == 2 * 30 and len(saved["3"]["drafts"]) == 2 * 30 and not errors
