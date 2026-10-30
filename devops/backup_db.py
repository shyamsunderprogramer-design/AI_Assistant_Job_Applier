"""Nightly backup of the job database and the person's own settings.

jobs.db holds every status the person set, every applied date and every
eligibility correction -- none of which can be scraped again. It is copied
with SQLite's online backup API (safe while the web app and a scrape hold it
open; a plain file copy of a WAL database can be torn), checked, compressed,
and kept for KEEP nights.

The person's own files (applicant.yaml, the answers, the search profile, the
lines added to the resume) go in the same night's folder: they are git-ignored,
so the repository is no backup for them. companies.db changes rarely and is
only copied when it has changed.

    python devops/backup_db.py [--to DIR] [--keep N]

The folder is BACKUP_DIR in .env, or "Job Applier backups" in Documents.
Prints one line of JSON for daily_pipeline.sh; exits non-zero on failure.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEEP = 14
# additions.yaml was listed under backend/config/, where it never was: the lines added to the
# resume, the general answers and the search profile went unsaved until 19 Oct 2026.
PERSONAL = ("backend/config/applicant.yaml", "backend/config/answers.yaml",
            "backend/config/general_answers.yaml", "backend/config/search_profile.yaml",
            "ml/resume/additions.yaml", "ml/resume/titles.yaml")


def destination(root: Path = ROOT) -> Path:
    """Where the nightly copies go: BACKUP_DIR (the environment, then .env), else a folder in
    Documents -- every Mac has one, where a second drive is one person's."""
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from backend.config.envfile import read_env
    chosen = (os.environ.get("BACKUP_DIR") or read_env(root / ".env").get("BACKUP_DIR") or "").strip()
    return Path(chosen).expanduser() if chosen else Path.home() / "Documents" / "Job Applier backups"


def copy_db(source: Path, target: Path) -> None:
    """Online backup to `target`, then a quick integrity check of the copy."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        # One step, not chunks: a chunked backup restarts whenever another process writes
        # between chunks, so during a scrape it may never finish. Under WAL a reader does
        # not block writers, so copying in one go costs the scrape nothing.
        src.backup(dst)
        ok = dst.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if ok != "ok":
        raise RuntimeError(f"backup of {source.name} failed its check: {ok}")


def gzip_file(path: Path) -> Path:
    out = path.with_suffix(path.suffix + ".gz")
    with open(path, "rb") as f, gzip.open(out, "wb", compresslevel=6) as g:
        shutil.copyfileobj(f, g, 1 << 20)
    path.unlink()
    return out


def latest(dest: Path, pattern: str) -> Path | None:
    found = sorted(dest.glob(f"*/{pattern}"))
    return found[-1] if found else None


def prune(dest: Path, keep: int) -> list[str]:
    nights = sorted(p for p in dest.iterdir() if p.is_dir() and p.name[:2] == "20")
    gone = []
    for old in nights[:-keep] if keep > 0 else []:
        # companies.db is only copied when it changes: never delete the last copy of it.
        if (old / "companies.db.gz").exists() and latest(dest, "companies.db.gz") == old / "companies.db.gz":
            continue
        shutil.rmtree(old)
        gone.append(old.name)
    return gone


def run(dest: Path | None = None, keep: int = KEEP, root: Path = ROOT) -> dict:
    dest = dest or destination(root)
    started = time.time()
    night = dest / datetime.now().strftime("%Y-%m-%d")
    night.mkdir(parents=True, exist_ok=True)
    saved = []

    copy_db(root / "data" / "jobs.db", night / "jobs.db")
    saved.append(gzip_file(night / "jobs.db").name)

    companies = root / "data" / "companies.db"
    last = latest(dest, "companies.db.gz")
    if companies.exists() and (last is None or companies.stat().st_mtime > last.stat().st_mtime):
        copy_db(companies, night / "companies.db")
        saved.append(gzip_file(night / "companies.db").name)

    for rel in PERSONAL:
        f = root / rel
        if f.exists():
            shutil.copy2(f, night / f.name)
            saved.append(f.name)

    size = sum(p.stat().st_size for p in night.iterdir())
    return {"ok": True, "task": "backup", "folder": str(night), "saved": saved,
            "mb": round(size / 1e6, 1), "seconds": round(time.time() - started, 1),
            "pruned": prune(dest, keep)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--to", type=Path, default=None)
    ap.add_argument("--keep", type=int, default=KEEP)
    args = ap.parse_args(argv)
    try:
        print(json.dumps(run(args.to, args.keep)))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "task": "backup", "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
