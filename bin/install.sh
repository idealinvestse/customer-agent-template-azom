#!/usr/bin/env bash
# Smart install wrapper: detects Ubuntu version and runs the right installer.
# Usage: sudo bash bin/install.sh [options...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash $0 $*" >&2
  exit 1
fi

if [[ ! -f /etc/os-release ]]; then
  echo "Unsupported OS (no /etc/os-release)" >&2
  exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release

case "${ID:-}:${VERSION_ID:-}" in
  ubuntu:26*|ubuntu:26.*)
    exec bash "${ROOT}/bin/install-ubuntu26.sh" "$@"
    ;;
  ubuntu:24.04|ubuntu:24.*)
    # Same path — install-ubuntu26 supports 24.04
    exec bash "${ROOT}/bin/install-ubuntu26.sh" "$@"
    ;;
  ubuntu:*)
    echo "Ubuntu ${VERSION_ID:-?} — using install-ubuntu26.sh (best effort)"
    exec bash "${ROOT}/bin/install-ubuntu26.sh" "$@"
    ;;
  *)
    echo "Distro ${ID:-unknown} ${VERSION_ID:-} is untested."
    echo "Continuing with install-ubuntu26.sh — Debian-family only."
    exec bash "${ROOT}/bin/install-ubuntu26.sh" "$@"
    ;;
esac
