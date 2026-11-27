#!/usr/bin/env bash
# Build the static demo (no backend) and optionally copy it somewhere.
#   scripts/record_demo.py          # once, against a running app (records demo-snapshot/data)
#   scripts/build_demo.sh [DEST]    # e.g. DEST=../showcase/demo/dilution
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f demo-snapshot/data/_index.json ]; then
  echo "No snapshot: run scripts/record_demo.py first" >&2
  exit 1
fi

rm -rf out
NEXT_DEMO=1 npx next build
mkdir -p out/demo-data
cp -R demo-snapshot/data/. out/demo-data/
touch out/.nojekyll   # GitHub Pages: serve _next/ as-is

if [ -n "${1:-}" ]; then
  rm -rf "$1"
  mkdir -p "$1"
  cp -R out/. "$1/"
  echo "Copied demo to $1"
fi
