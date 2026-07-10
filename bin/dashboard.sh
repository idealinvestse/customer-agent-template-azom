#!/usr/bin/env bash
# Intern webbdashboard för Jonatan (lösenordsskyddad, read-only).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT}/skills${PYTHONPATH:+:$PYTHONPATH}"
export AZOM_CONFIG_DIR="${AZOM_CONFIG_DIR:-$ROOT/config}"
export AZOM_DATA_DIR="${AZOM_DATA_DIR:-$ROOT/.azom-data}"

cd "$ROOT"
exec bash "$ROOT/bin/start-dashboard.sh" "$@"
