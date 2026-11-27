#!/usr/bin/env bash
# Local AI cron test: Postgres + Claude → same job as Render dilution-ai-daily-sync.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TICKER="${1:-POET}"
shift 2>/dev/null || true

# Cron jobs require Postgres (not SQLite).
# The Python job loads .env with python-dotenv. Do not source it as shell code:
# valid dotenv values such as SEC_USER_AGENT=Name (email) break bash parsing.
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://USER:PASSWORD@localhost:5432/buyback_tracker}"
if [[ "$DATABASE_URL" == *sqlite* ]]; then
  echo "Overriding SQLite DATABASE_URL for cron test → local Postgres."
  export DATABASE_URL="postgresql+asyncpg://USER:PASSWORD@localhost:5432/buyback_tracker"
fi

export REPO_ROOT="$ROOT"
export PYTHONPATH="$ROOT/backend"

if [[ -x "$ROOT/backend/venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/backend/venv/bin/python"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

if ! docker compose ps db --status running 2>/dev/null | grep -q running; then
  echo "Starting local Postgres (docker compose up -d db)..."
  docker compose up -d db
  echo "Waiting for Postgres..."
  for _ in $(seq 1 30); do
    if docker compose exec -T db pg_isready -U postgres -d buyback_tracker >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

echo "AI dilution test: ticker=$TICKER db=${DATABASE_URL%%@*}@..."
echo "Tip: run non-AI first if stats are empty: ./scripts/run_non_ai_sync.sh --ticker $TICKER"
echo ""

cd "$ROOT/backend"
exec "$PYTHON_BIN" -m app.jobs.ai_dilution_daily_sync --ticker "$TICKER" "$@"
