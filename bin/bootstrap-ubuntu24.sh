#!/usr/bin/env bash
# Compatibility wrapper — full auto-install lives in install-ubuntu26.sh
# (supports Ubuntu 26.x and 24.04 LTS).
#
# Usage: sudo bash bin/bootstrap-ubuntu24.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "NOTE: bootstrap-ubuntu24.sh now delegates to install-ubuntu26.sh"
exec bash "${ROOT}/bin/install-ubuntu26.sh" "$@"
