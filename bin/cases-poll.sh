#!/usr/bin/env bash
# Poll functional mailboxes and create support cases.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT}/skills${PYTHONPATH:+:$PYTHONPATH}"
export AZOM_CONFIG_DIR="${AZOM_CONFIG_DIR:-$ROOT/config}"
export AZOM_DATA_DIR="${AZOM_DATA_DIR:-$ROOT/.azom-data}"

cd "$ROOT"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

MOCK_FLAG=()
if [[ "${AZOM_USE_MOCK:-1}" == "1" ]]; then
  MOCK_FLAG=(--mock)
fi

export AZOM_LOG_DIR="${AZOM_LOG_DIR:-$ROOT/logs}"
export AZOM_LOG_NAME="${AZOM_LOG_NAME:-cases-poll}"

# Profile log only — never auto-enable null-send (Oscar sets AZOM_NULL_SEND in .env).
_null_raw="$(printf '%s' "${AZOM_NULL_SEND:-}" | tr '[:upper:]' '[:lower:]')"
_null_label=off
case "${_null_raw}" in
  1|true|yes|on) _null_label=on ;;
esac
echo "cases-poll: mock=${AZOM_USE_MOCK:-1} null_send=${_null_label}" >&2

exec "$PY" -m ecom_ops "${MOCK_FLAG[@]}" cases poll "$@"
