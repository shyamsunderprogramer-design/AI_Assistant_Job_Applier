"""The cloud's saved database when the cache has none: from the newest verified result (26 Nov 2026).

The saved database lives in the Actions cache, which GitHub may evict. With it would go what a run needs to stay
continuous with the ones before: each board's last read (who is read first), the scrape log (each site's last
refusal, so a refusing site is only probed), the boards' inventories and the boards still to read. All of it is in
every verified result too (devops/discovery_result.py: the merged database, locked and signed, kept 7 days). So when
the cache restores nothing, the newest result of this workflow that is still there is downloaded, unlocked, checked
exactly as the Mac checks it (signature, plan, outcomes, database hash and integrity) and no older than MAX_AGE, and
its database becomes the saved one. Its run id and age are said, and carried into the coverage. Nothing usable: a
cold start, said as one (devops/discovery_merge.py), never presented as continuous.

    python -m devops.discovery_restore --dest data/jobs.db --repo owner/name     (lines for $GITHUB_OUTPUT)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ARTIFACT = "discovery-result-v1"
LOCKED = "discovery-result.tar.enc"
WORKFLOW = "daily-scrape.yml"
MAX_AGE = timedelta(days=6)
CIPHER = ["enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256"]


def _gh(*args: str, timeout: int = 600) -> str:
    done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(f"gh {args[0]} failed")            # never the token or the output
    return done.stdout


def candidates(repo: str, now: datetime) -> list[dict]:
    """This workflow's runs whose verified result is still kept, newest first."""
    runs = json.loads(_gh("api", f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?status=completed&per_page=30"))
    out = []
    for r in runs.get("workflow_runs") or []:
        created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        if now - created > MAX_AGE:
            continue
        arts = json.loads(_gh("api", f"repos/{repo}/actions/runs/{r['id']}/artifacts?name={ARTIFACT}"))
        if any(a.get("name") == ARTIFACT and not a.get("expired") for a in arts.get("artifacts") or []):
            out.append({"id": r["id"], "created": created})
    return sorted(out, key=lambda r: r["created"], reverse=True)


def unpack(locked: Path, into: Path, passphrase: str) -> tuple[dict, Path]:
    """Unlocked and unpacked: exactly result.json and jobs.db, plain files, nothing else."""
    raw = into / "result.tar"
    subprocess.run(["openssl", *CIPHER, "-in", str(locked), "-out", str(raw), "-pass", "env:RESTORE_PASSPHRASE"],
                   check=True, capture_output=True, env={**os.environ, "RESTORE_PASSPHRASE": passphrase})
    with tarfile.open(raw) as tar:
        members = tar.getmembers()
        if sorted(m.name for m in members) != ["jobs.db", "result.json"] or not all(m.isfile() for m in members):
            raise ValueError("the result does not hold exactly its two files")
        for m in members:
            src = tar.extractfile(m)
            if src is None:
                raise ValueError("the result holds an unreadable member")
            with src, open(into / m.name, "wb") as dst:
                shutil.copyfileobj(src, dst)
    raw.unlink()
    return json.loads((into / "result.json").read_text()), into / "jobs.db"


def restore(dest: Path, repo: str, passphrase: str, now: datetime | None = None, download=None) -> dict:
    """The newest usable verified result's database into `dest`. Returns {restored_from, finished_at, age_hours}
    or {restored_from: None, why}. `download(run_id, folder)` puts the locked result in `folder` (tests)."""
    from devops.discovery_result import ResultError, verify
    now = now or datetime.now(timezone.utc)
    if dest.exists():
        return {"restored_from": None, "why": "a saved database is there already"}
    if not passphrase:
        return {"restored_from": None, "why": "no passphrase"}
    download = download or (lambda run_id, folder: _gh("run", "download", str(run_id), "--repo", repo,
                                                       "--name", ARTIFACT, "--dir", str(folder)))
    tried = []
    for run in candidates(repo, now) if repo else []:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            try:
                download(run["id"], folder)
                result, db = unpack(folder / LOCKED, folder, passphrase)
                verify(result, db, passphrase)
                finished = datetime.fromisoformat(str((result.get("run") or {}).get("finished_at")).replace("Z", "+00:00"))
                if now - finished > MAX_AGE:
                    raise ValueError("too old")
            except (RuntimeError, ValueError, ResultError, OSError, tarfile.TarError, subprocess.CalledProcessError) as exc:
                tried.append(f"{run['id']}: {type(exc).__name__}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(db), str(dest))
            return {"restored_from": str(run["id"]), "finished_at": finished.isoformat(timespec="seconds"),
                    "age_hours": round((now - finished).total_seconds() / 3600, 1)}
    return {"restored_from": None, "why": "no usable verified result" + (f" ({'; '.join(tried)})" if tried else "")}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--dest", type=Path, required=True)
    ap.add_argument("--repo", required=True)
    args = ap.parse_args(argv)
    got = restore(args.dest, args.repo, os.environ.get("PASSPHRASE", ""))
    out = os.environ.get("GITHUB_OUTPUT")
    line = f"restored_from={got.get('restored_from') or ''}\n"
    if out:
        with open(out, "a") as fh:
            fh.write(line)
    else:
        sys.stdout.write(line)
    if got.get("restored_from"):
        print(f"::notice::The cache held no saved database: restored from run {got['restored_from']}'s verified "
              f"result ({got['age_hours']} h old).")
    else:
        print(f"::warning::Cold start: no saved database and {got.get('why')}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
