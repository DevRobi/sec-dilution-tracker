#!/usr/bin/env bash
# Run a cron job inside the worker image against docker-compose Postgres.
# Usage: ./scripts/run_worker_in_docker.sh non_ai
#        ./scripts/run_worker_in_docker.sh ai --ticker POET
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JOB="${1:-}"
shift || true
case "$JOB" in
  non_ai) MODULE="app.jobs.non_ai_daily_sync" ;;
  ai)     MODULE="app.jobs.ai_dilution_daily_sync" ;;
  *)
    echo "Usage: $0 {non_ai|ai} [--ticker TICKER ...]"
    exit 1
    ;;
esac
cd "$ROOT"
docker compose --profile jobs run --rm worker \
  python -m "$MODULE" "$@"
