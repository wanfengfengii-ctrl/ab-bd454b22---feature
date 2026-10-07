#!/bin/sh
set -e

mkdir -p "$(dirname "$DB_PATH")"

exec uvicorn app.main:app \
  --host "${GATEWAY_HOST:-0.0.0.0}" \
  --port "${GATEWAY_PORT:-8000}"
