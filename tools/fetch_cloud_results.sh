#!/usr/bin/env bash
# Bring the nightly scrape's results down from GitHub Actions.
#
#   bash tools/fetch_cloud_results.sh
#
# The runner encrypts data/jobs.db before uploading, because an artifact on a
# public repository can be downloaded by anyone. This decrypts it with the same
# passphrase and puts it in place — keeping a copy of whatever was there first,
# because the local database may hold application statuses the runner never saw.

set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT" || exit 1

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

if [ -f data/jobs.db ]; then
  BACKUP="data/jobs.db.local-$(date +%Y%m%d-%H%M%S)"
  cp data/jobs.db "$BACKUP"
  echo "Your existing database kept at $BACKUP"
  echo "  (it may hold application statuses the runner never saw)"
fi

mv "$TMP/jobs.db" data/jobs.db
echo "Done: $ROWS postings, from run $RUN."
