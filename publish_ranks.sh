#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

python3 macro_factor_data.py
python3 publish_rank_history.py
git add docs

if git diff --cached --quiet -- docs; then
  echo "No rank changes to publish."
  exit 0
fi

git commit -m "Publish V2 rank snapshots" -- docs
git push origin HEAD:main
