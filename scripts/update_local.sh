#!/bin/zsh
set -euo pipefail

export PATH="/Users/mf/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO_DIR="/Users/mf/Desktop/Codex/Software/HomeTV-Sources"
AUDIT_JSON="$(mktemp /tmp/hometv-source-update.XXXXXX.json)"

cd "$REPO_DIR"
git pull --ff-only
python3 scripts/audit_streams.py \
  --m3u candidates.m3u \
  --output "$AUDIT_JSON" \
  --concurrency 6
python3 scripts/publish.py --audit "$AUDIT_JSON"

git add curated.m3u manifest.json health-report.json health-state.json
if git diff --cached --quiet; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') No source changes"
else
  git commit -m "chore: refresh validated IPTV sources"
  git push
fi
