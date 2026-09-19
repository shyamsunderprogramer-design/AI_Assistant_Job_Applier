"""Read and write .env without losing what is already in it.

Editing this file by hand is how a key gets corrupted. It happened here: a
value was appended to a .env whose last line had no trailing newline, and
`RESULTS_PASSPHRASE=` was glued onto the end of MAIL_APP_PASSWORD. Nothing
failed loudly -- mail authentication just stopped working, silently, for a
day. So every write from here ends every line with a newline, and a merge
keeps the comments and the ordering that were already there.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# KEY=value, tolerating `export ` and surrounding space. Anything else -- a
# comment, a blank line, a line someone hand-wrote badly -- is kept verbatim
# rather than silently dropped.
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if value[0] == '"':
            # Double quotes escape, single quotes are literal -- the same rule
            # python-dotenv applies when it reads the file back.
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return value


def _quote(value: str) -> str:
    """Quote only when the value would not survive being read back plainly."""
    if value == "" or re.fullmatch(r"[A-Za-z0-9_@./:+-]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_env(path: Path | str) -> dict[str, str]:
    """Every KEY=value in the file. Missing file means no settings, not an error."""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match and not line.lstrip().startswith("#"):
            values[match.group(1)] = _unquote(match.group(2))
    return values


def write_env(path: Path | str, updates: dict[str, str | None]) -> list[str]:
    """Merge `updates` into the file and return the keys actually changed.

    A value of None removes the key. A key already present is rewritten where
    it stands, so comments explaining it stay attached to it; a new key is
    appended. Existing lines this function does not understand are copied
    through untouched -- it is the user's file, not ours.

    The file is written with owner-only permissions, because it holds
    credentials and this is the one place in the tool that creates it.
    """
    path = Path(path)
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        original = ""

    existing = read_env(path)
    changed = [
        key for key, value in updates.items()
        if (value is None and key in existing) or
           (value is not None and existing.get(key) != value)
    ]
    if not changed:
        return []

    remaining = dict(updates)
    lines: list[str] = []
    for line in original.splitlines():
        match = _ASSIGNMENT.match(line)
        key = match.group(1) if match and not line.lstrip().startswith("#") else None
        if key is None or key not in remaining:
            lines.append(line)
            continue
        value = remaining.pop(key)
        if value is not None:                 # None deletes the line entirely
            lines.append(f"{key}={_quote(value)}")

    appended = [(k, v) for k, v in remaining.items() if v is not None]
    if appended:
        if lines and lines[-1].strip():
            lines.append("")
        for key, value in appended:
            lines.append(f"{key}={_quote(value)}")

    # The trailing newline is the whole point: without it the next append
    # lands on the end of the last value instead of on its own line.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass                                  # best effort; some filesystems refuse
    return changed
