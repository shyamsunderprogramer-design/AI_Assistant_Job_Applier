"""How far a resume rewrite has got, for the job page's progress bar.

The rewrite runs as its own command, so it writes where it is to a small file
and the page reads it. Rounds share the bar: with three rounds, each owns a
third, and a round that reaches the target skips the rest straight to 100%.
Nothing here can fail a rewrite: a file that cannot be written is skipped.
"""

from __future__ import annotations

import json
import time

from backend.config.loader import PROJECT_ROOT

FOLDER = PROJECT_ROOT / "data" / "progress"

# Where each step of one round starts, as a share of that round.
STEPS = {
    "reading": (0.0, "Reading your resume and the posting"),
    "writing": (0.05, "Writing the resume"),
    "fitting": (0.70, "Fitting it to the page limit"),
    "saving": (0.82, "Saving the .docx and PDF"),
    "checking": (0.90, "Checking the match with the posting"),
}

_state = {"job": None, "rounds": 1, "round": 0}


def path_for(job_id: int):
    return FOLDER / f"tailor-{job_id}.json"


def begin(job_id: int, rounds: int) -> None:
    _state.update(job=job_id, rounds=max(1, rounds), round=0)
    step("reading")


def next_round(number: int) -> None:
    _state["round"] = number
    step("reading")


def step(name: str) -> None:
    if _state["job"] is None:
        return
    start, label = STEPS[name]
    share = 100 / _state["rounds"]
    pct = share * (_state["round"] + start)
    if _state["rounds"] > 1:
        label = f"Round {_state['round'] + 1} of {_state['rounds']}: {label[0].lower()}{label[1:]}"
    # The next step's start: the page eases toward it while a slow step runs.
    later = [s for s, _ in STEPS.values() if s > start]
    upto = share * (_state["round"] + (min(later) if later else 1.0))
    _write({"pct": round(pct, 1), "upto": round(upto, 1), "label": label})


def finish(label: str = "Done") -> None:
    if _state["job"] is not None:
        _write({"pct": 100, "upto": 100, "label": label, "done": True})
    _state["job"] = None


def _write(data: dict) -> None:
    try:
        FOLDER.mkdir(parents=True, exist_ok=True)
        path_for(_state["job"]).write_text(json.dumps({**data, "at": time.time()}))
    except OSError:
        pass


def read(job_id: int) -> dict | None:
    try:
        return json.loads(path_for(job_id).read_text())
    except (OSError, ValueError):
        return None
