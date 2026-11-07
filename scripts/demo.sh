#!/usr/bin/env bash
# The four demo beats against a real mandate service and real PayPal sandbox.
# Makes ZERO LLM calls. Starts the service if it isn't running, stops it after.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then set -a; source .env; set +a; fi
export MANDATE_URL="${MANDATE_URL:-http://localhost:8000}"
PY="${PY:-.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3
mkdir -p data

started=""
if ! curl -fsS "$MANDATE_URL/healthz" >/dev/null 2>&1; then
  "$PY" -m uvicorn --factory mandate.main:app_from_env --port "${MANDATE_URL##*:}" \
    >data/demo-server.log 2>&1 &
  started=$!
  trap '[[ -n "$started" ]] && kill "$started" 2>/dev/null' EXIT
  for _ in $(seq 50); do
    curl -fsS "$MANDATE_URL/healthz" >/dev/null 2>&1 && break
    sleep 0.2
  done
fi

"$PY" scripts/demo.py
