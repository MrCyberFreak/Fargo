#!/usr/bin/env bash
# Shallow-clone (or update) sibling pool-data repos into _ref/ so Fargo can read
# their COMMITTED data without ever touching a sibling's local working copy (the
# hard project boundary in CLAUDE.md / docs/cross-league-identity.md). Nothing
# under _ref/ is committed, and upstream code is NEVER executed -- only committed
# data files are parsed.
#
# Usage: scripts/sync_ref.sh NAPA [APA-Scraper bca ...]
# Auth:  export REF_TOKEN=<github token> for private sibling repos (optional;
#        the per-repo GITHUB_TOKEN in Actions does NOT grant access to OTHER repos,
#        so a PAT secret is needed in CI when a sibling is private -- see M5).
set -euo pipefail

OWNER="MrCyberFreak"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REF_DIR="$ROOT/_ref"
mkdir -p "$REF_DIR"

auth=""
if [ -n "${REF_TOKEN:-}" ]; then
  auth="x-access-token:${REF_TOKEN}@"
fi

for name in "$@"; do
  dest="$REF_DIR/$name"
  url="https://${auth}github.com/${OWNER}/${name}.git"
  if [ -d "$dest/.git" ]; then
    echo "updating _ref/$name"
    git -C "$dest" fetch --depth 1 origin
    git -C "$dest" reset --hard FETCH_HEAD
  else
    echo "cloning _ref/$name"
    git clone --depth 1 "$url" "$dest"
  fi
done
