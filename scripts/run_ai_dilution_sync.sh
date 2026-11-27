#!/usr/bin/env bash
# AI dilution cron locally → Postgres via Claude (claude-code CLI or Anthropic API) or Ollama (see .env).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
export PYTHONPATH="$ROOT/backend"
export REPO_ROOT="$ROOT"
exec python3 -m app.jobs.ai_dilution_daily_sync "$@"
