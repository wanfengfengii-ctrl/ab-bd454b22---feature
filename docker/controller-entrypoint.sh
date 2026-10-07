#!/bin/sh
set -e

exec uvicorn app.simulator:app \
  --host 0.0.0.0 \
  --port "${CONTROLLER_PORT:-8080}"
