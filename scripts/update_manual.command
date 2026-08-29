#!/bin/zsh
set -euo pipefail

export PATH="/Users/mf/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO_DIR="/Users/mf/Desktop/Codex/Software/HomeTV-Sources"
AUDIT_FIRST="$(mktemp /tmp/hometv-source-first.XXXXXX.json)"
AUDIT_SECOND="$(mktemp /tmp/hometv-source-second.XXXXXX.json)"
AUDIT_MERGED="$(mktemp /tmp/hometv-source-merged.XXXXXX.json)"

cd "$REPO_DIR"
git pull --ff-only
python3 scripts/audit_streams.py \
  --m3u candidates.m3u \
  --output "$AUDIT_FIRST" \
  --concurrency 6
echo "Waiting 60 seconds before the required second pass..."
sleep 60
python3 scripts/audit_streams.py \
  --m3u candidates.m3u \
  --output "$AUDIT_SECOND" \
  --concurrency 6
python3 scripts/merge_audits.py \
  --first "$AUDIT_FIRST" \
  --second "$AUDIT_SECOND" \
  --output "$AUDIT_MERGED"
python3 scripts/publish.py --audit "$AUDIT_MERGED"

git add curated.m3u manifest.json health-report.json health-state.json
if git diff --cached --quiet; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') No source changes"
else
  git commit -m "chore: refresh validated IPTV sources"
  git push
fi
