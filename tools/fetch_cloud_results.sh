#!/usr/bin/env bash
# Bring the nightly scrape's results down from GitHub Actions.
#
#   bash tools/fetch_cloud_results.sh
#
# The runner encrypts data/jobs.db before uploading, because an artifact on a
# public repository can be downloaded by anyone. This decrypts it with the same
# passphrase and MERGES it into the local database — never replaces it. The
# cloud knows about postings; this machine knows about job-alert postings,
# boards the probe found, your application statuses, and the probe's memory of
# what it has already asked. Replacing throws all of that away.

set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT" || exit 1
export PYTHONPATH="$PROJECT"

command -v gh >/dev/null 2>&1 || { echo "gh is not installed:  brew install gh"; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "openssl is missing"; exit 1; }

# Same passphrase as the GitHub secret. Read from .env, which is gitignored.
if [ -z "${RESULTS_PASSPHRASE:-}" ] && [ -f .env ]; then
  RESULTS_PASSPHRASE=$(sed -n 's/^RESULTS_PASSPHRASE=//p' .env | head -1 | tr -d "\"'")
  export RESULTS_PASSPHRASE
fi

if [ -z "${RESULTS_PASSPHRASE:-}" ]; then
  echo "RESULTS_PASSPHRASE is not set."
  echo "  Put the same value in .env that you gave the GitHub secret:"
  echo "    echo 'RESULTS_PASSPHRASE=your-passphrase' >> .env"
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Finding the most recent successful scrape..."
RUN=$(gh run list --workflow "Daily scrape" --status success --limit 1 \
        --json databaseId --jq '.[0].databaseId' 2>/dev/null)
if [ -z "$RUN" ]; then
  echo "No successful run found yet. Start one with:"
  echo "    gh workflow run 'Daily scrape'"
  exit 1
fi

if ! gh run download "$RUN" --name jobs-db-encrypted --dir "$TMP" 2>/dev/null; then
  echo "Run $RUN has no artifact."
  # A run only uploads if RESULTS_PASSPHRASE existed WHEN IT STARTED. Saying
  # "the secret is not set" is wrong and confusing once it is -- the first
  # time this happened the secret had been added 54 minutes after the run
  # began, so the advice sent someone to re-set a secret that was already
  # correct. Check, and say which of the two it actually is.
  if gh secret list 2>/dev/null | grep -q RESULTS_PASSPHRASE; then
    SET_AT=$(gh secret list 2>/dev/null | awk '/RESULTS_PASSPHRASE/{print $2}')
    RUN_AT=$(gh run view "$RUN" --json createdAt --jq .createdAt 2>/dev/null)
    echo "  RESULTS_PASSPHRASE IS set (at $SET_AT), but this run started at"
    echo "  $RUN_AT. A run only uploads if the secret existed when it began."
    echo "  Start a fresh one:  gh workflow run 'Daily scrape'"
  else
    echo "  RESULTS_PASSPHRASE is not set as a GitHub secret, so the upload"
    echo "  step was skipped. Set it, then run the workflow again:"
    echo "    gh secret set RESULTS_PASSPHRASE"
    echo "    gh workflow run 'Daily scrape'"
  fi
  exit 1
fi

echo "Decrypting..."
if ! openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
       -in "$TMP/jobs.db.enc" -out "$TMP/jobs.db" -pass env:RESULTS_PASSPHRASE 2>/dev/null; then
  echo "Could not decrypt — the passphrase here does not match the one on GitHub."
  exit 1
fi

# Check it before overwriting anything: a truncated download must never
# replace a working database.
ROWS=$(sqlite3 "$TMP/jobs.db" "SELECT COUNT(*) FROM jobs;" 2>/dev/null)
if [ -z "$ROWS" ]; then
  echo "The decrypted file is not a valid database — yours is untouched."
  exit 1
fi

# MERGE, never replace. Replacing looked reasonable and was badly wrong the
# first time it ran for real: the cloud knows about postings, the laptop knows
# about everything else, and overwriting cost 35 job-alert postings, 375
# boards, 32 application statuses and 138,438 probe records -- to gain nine
# postings, because the two databases overlap almost entirely. Losing
# probe_log alone would have re-sent a hundred and thirty-eight thousand
# requests to other people's servers to relearn what was already known.
if [ ! -f data/jobs.db ]; then
  mv "$TMP/jobs.db" data/jobs.db
  echo "Done: $ROWS postings from run $RUN (no local database existed)."
  exit 0
fi

BACKUP="data/jobs.db.local-$(date +%Y%m%d-%H%M%S)"
cp data/jobs.db "$BACKUP"
echo "Local database backed up to $BACKUP"

if ! "$PROJECT/.venv/bin/python" -m tools.merge_cloud "$TMP/jobs.db"; then
  echo "Merge failed — your database is untouched (backup at $BACKUP)."
  exit 1
fi
echo "Done: merged run $RUN ($ROWS postings scanned)."
