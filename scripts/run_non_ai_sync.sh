#!/usr/bin/env bash
# Non-AI cron locally → Postgres (same code path as Render worker cron).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
export PYTHONPATH="$ROOT/backend"
export REPO_ROOT="$ROOT"
exec python3 -m app.jobs.non_ai_daily_sync "$@"
