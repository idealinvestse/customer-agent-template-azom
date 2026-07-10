#!/usr/bin/env bash
# Start Flask dashboard on :8080 (password-protected for Jonatan).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT}/skills${PYTHONPATH:+:$PYTHONPATH}"
export AZOM_CONFIG_DIR="${AZOM_CONFIG_DIR:-$ROOT/config}"
export AZOM_DATA_DIR="${AZOM_DATA_DIR:-$ROOT/.azom-data}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python not found" >&2
  exit 1
fi

# Flask is optional for core ecom-ops; install if missing
"$PY" -c "import flask" 2>/dev/null || "$PY" -m pip install -q flask

HOST="${DASHBOARD_HOST:-127.0.0.1}"
PORT="${DASHBOARD_PORT:-8080}"
echo "Dashboard live på http://${HOST}:${PORT} (lösenordsskyddad)"
exec "$PY" infrastructure/dashboard/app.py
