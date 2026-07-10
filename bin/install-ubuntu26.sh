#!/usr/bin/env bash
# =============================================================================
# Azom Ops Agent — fully automatic install for Ubuntu 26.x (and 24.04 LTS)
#
# One-shot: installs OS deps, clones/copies the project, venv, systemd units,
# firewall, logrotate, runs smoke tests, and starts services.
#
# Usage (as root on a fresh VPS):
#   curl -fsSL https://raw.githubusercontent.com/idealinvestse/customer-agent-template-azom/main/bin/install-ubuntu26.sh | sudo bash
#
# Or from a cloned repo:
#   sudo bash bin/install-ubuntu26.sh
#   sudo bash bin/install-ubuntu26.sh --repo https://github.com/idealinvestse/customer-agent-template-azom.git
#   sudo bash bin/install-ubuntu26.sh --mock --no-start   # CI / dry install
#
# Environment overrides:
#   AZOM_USER, AZOM_APP_DIR, AZOM_REPO_URL, AZOM_REPO_BRANCH, AZOM_SSH_PORT
#   AZOM_USE_MOCK, DASHBOARD_PASSWORD, DASHBOARD_OSCAR_PASSWORD
#   TELEGRAM_BOT_TOKEN, SKIP_DOCKER=1, SKIP_UFW=1
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
APP_USER="${AZOM_USER:-azom}"
APP_DIR="${AZOM_APP_DIR:-/opt/azom-agent}"
DATA_DIR="${AZOM_DATA_DIR:-/var/lib/azom}"
LOG_DIR="${AZOM_LOG_DIR:-/var/log/azom}"
REPO_URL="${AZOM_REPO_URL:-https://github.com/idealinvestse/customer-agent-template-azom.git}"
REPO_BRANCH="${AZOM_REPO_BRANCH:-main}"
SSH_PORT="${AZOM_SSH_PORT:-22}"
USE_MOCK="${AZOM_USE_MOCK:-0}"
SKIP_DOCKER="${SKIP_DOCKER:-0}"
SKIP_UFW="${SKIP_UFW:-0}"
START_SERVICES=1
RUN_SMOKE=1
INSTALL_DOCKER=1

log()  { echo -e "\n\033[1;32m==>\033[0m $*"; }
warn() { echo -e "\033[1;33mWARN:\033[0m $*" >&2; }
die()  { echo -e "\033[1;31mERROR:\033[0m $*" >&2; exit 1; }
ok()   { echo -e "\033[1;32mOK\033[0m  $*"; }

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \?//'
  exit 0
}

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_URL="$2"; shift 2 ;;
    --branch) REPO_BRANCH="$2"; shift 2 ;;
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --user) APP_USER="$2"; shift 2 ;;
    --ssh-port) SSH_PORT="$2"; shift 2 ;;
    --mock) USE_MOCK=1; shift ;;
    --no-docker) INSTALL_DOCKER=0; SKIP_DOCKER=1; shift ;;
    --no-ufw) SKIP_UFW=1; shift ;;
    --no-start) START_SERVICES=0; shift ;;
    --no-smoke) RUN_SMOKE=0; shift ;;
    -h|--help) usage ;;
    *) die "Unknown arg: $1 (try --help)" ;;
  esac
done

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
[[ "$(id -u)" -eq 0 ]] || die "Run as root: sudo bash $0"

if [[ ! -f /etc/os-release ]]; then
  die "/etc/os-release missing — not a supported Linux distro"
fi
# shellcheck disable=SC1091
. /etc/os-release
OS_ID="${ID:-unknown}"
OS_VERSION="${VERSION_ID:-unknown}"
OS_CODENAME="${VERSION_CODENAME:-unknown}"

case "$OS_ID" in
  ubuntu)
    case "$OS_VERSION" in
      26.*|26) ok "Ubuntu $OS_VERSION ($OS_CODENAME) — primary target" ;;
      24.04|24.*) warn "Ubuntu $OS_VERSION detected — supported (same install path as 26)" ;;
      22.04) warn "Ubuntu 22.04 works but is not the primary target" ;;
      *) warn "Ubuntu $OS_VERSION is untested; continuing" ;;
    esac
    ;;
  *)
    warn "Distro is $OS_ID $OS_VERSION (expected Ubuntu 26/24). Continuing carefully."
    ;;
esac

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

# Resolve script / source root (when run from a checkout)
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
if [[ -f "$SCRIPT_PATH" ]]; then
  SCRIPT_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
else
  SCRIPT_ROOT=""
fi

# ---------------------------------------------------------------------------
# 1) System packages
# ---------------------------------------------------------------------------
log "Updating apt and installing base packages"
apt-get update -qq
apt-get install -y -qq \
  ca-certificates curl gnupg lsb-release git rsync \
  python3 python3-pip python3-venv python3-dev python3-full \
  build-essential openssh-client \
  ufw fail2ban unattended-upgrades \
  jq logrotate openssl acl

# Ensure python3 is available
command -v python3 >/dev/null || die "python3 not installed"
PY_VER="$(python3 -c 'import sys; print("%d.%d"%sys.version_info[:2])')"
ok "Python $PY_VER"

# ---------------------------------------------------------------------------
# 2) Docker (optional)
# ---------------------------------------------------------------------------
if [[ "$INSTALL_DOCKER" == "1" && "$SKIP_DOCKER" != "1" ]]; then
  log "Installing Docker Engine"
  if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
      chmod a+r /etc/apt/keyrings/docker.gpg
    fi
    ARCH="$(dpkg --print-architecture)"
    # Prefer VERSION_CODENAME; fall back for pre-release / unknown
    DOCKER_CODENAME="$OS_CODENAME"
    if [[ -z "$DOCKER_CODENAME" || "$DOCKER_CODENAME" == "unknown" ]]; then
      DOCKER_CODENAME="noble"  # Ubuntu 24.04 codename — works for many 26 pre-releases
    fi
    echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${DOCKER_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq || {
      warn "Docker apt repo failed for codename=${DOCKER_CODENAME}; retrying with noble"
      echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu noble stable" \
        > /etc/apt/sources.list.d/docker.list
      apt-get update -qq
    }
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
      docker-buildx-plugin docker-compose-plugin || {
      warn "Docker install failed — continuing without Docker (venv path still works)"
      INSTALL_DOCKER=0
    }
  fi
  if command -v docker >/dev/null 2>&1; then
    systemctl enable --now docker
    ok "Docker $(docker --version 2>/dev/null | head -1)"
  fi
else
  log "Skipping Docker install"
fi

# ---------------------------------------------------------------------------
# 3) App user + directories
# ---------------------------------------------------------------------------
log "Creating user ${APP_USER} and directories"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash \
    --home-dir "/home/${APP_USER}" "$APP_USER"
fi
if command -v docker >/dev/null 2>&1; then
  usermod -aG docker "$APP_USER" || true
fi

mkdir -p "$APP_DIR" "$DATA_DIR" "$LOG_DIR" \
  "${DATA_DIR}/oauth" "${APP_DIR}/logs" "${APP_DIR}/.azom-data"
chown -R "${APP_USER}:${APP_USER}" "$APP_DIR" "$DATA_DIR" "$LOG_DIR"
chmod 750 "$DATA_DIR" "$LOG_DIR"

# ---------------------------------------------------------------------------
# 4) Project code
# ---------------------------------------------------------------------------
log "Installing project into ${APP_DIR}"

deploy_from_checkout() {
  local src="$1"
  log "Syncing from local checkout: $src"
  rsync -a --delete \
    --exclude .git \
    --exclude .venv \
    --exclude .azom-data \
    --exclude .azom-data-test \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude .env \
    --exclude logs \
    "${src}/" "${APP_DIR}/"
}

if [[ -n "$SCRIPT_ROOT" && -f "${SCRIPT_ROOT}/pyproject.toml" ]]; then
  deploy_from_checkout "$SCRIPT_ROOT"
elif [[ -f "${APP_DIR}/pyproject.toml" && -d "${APP_DIR}/.git" ]]; then
  log "Updating existing git checkout"
  sudo -u "$APP_USER" git -C "$APP_DIR" fetch --all --prune
  sudo -u "$APP_USER" git -C "$APP_DIR" checkout "$REPO_BRANCH"
  sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only origin "$REPO_BRANCH" || true
elif [[ -n "$REPO_URL" ]]; then
  if [[ -d "${APP_DIR}/.git" ]]; then
    log "Pulling ${REPO_URL} (${REPO_BRANCH})"
    sudo -u "$APP_USER" git -C "$APP_DIR" remote set-url origin "$REPO_URL" || true
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch origin
    sudo -u "$APP_USER" git -C "$APP_DIR" checkout "$REPO_BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only origin "$REPO_BRANCH" || true
  else
    # Empty or non-git dir — re-clone cleanly
    if [[ -n "$(ls -A "$APP_DIR" 2>/dev/null || true)" ]]; then
      # Keep existing .env if present
      if [[ -f "${APP_DIR}/.env" ]]; then
        cp "${APP_DIR}/.env" /tmp/azom-env.backup
      fi
      find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name '.env' -exec rm -rf {} +
    fi
    log "Cloning ${REPO_URL} @ ${REPO_BRANCH}"
    sudo -u "$APP_USER" git clone --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" "$APP_DIR"
    if [[ -f /tmp/azom-env.backup ]]; then
      mv /tmp/azom-env.backup "${APP_DIR}/.env"
      chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
    fi
  fi
else
  die "No source: set AZOM_REPO_URL or run from a project checkout"
fi

[[ -f "${APP_DIR}/pyproject.toml" ]] || die "pyproject.toml missing in ${APP_DIR}"
chown -R "${APP_USER}:${APP_USER}" "$APP_DIR"
chmod +x "${APP_DIR}/bin/"*.sh 2>/dev/null || true

# ---------------------------------------------------------------------------
# 5) Python venv + package
# ---------------------------------------------------------------------------
log "Creating venv and installing Python package"
sudo -u "$APP_USER" bash <<EOF
set -euo pipefail
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install -U pip wheel setuptools
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
.venv/bin/python -c "import ecom_ops; print('ecom_ops', ecom_ops.__version__)"
EOF
ok "Package installed in ${APP_DIR}/.venv"

# ---------------------------------------------------------------------------
# 6) .env configuration
# ---------------------------------------------------------------------------
log "Configuring environment (.env)"
ENV_FILE="${APP_DIR}/.env"
EXAMPLE="${APP_DIR}/.env.example"

gen_password() {
  openssl rand -base64 24 | tr -d '/+=' | head -c 24
}

if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$EXAMPLE" ]]; then
    cp "$EXAMPLE" "$ENV_FILE"
  else
    touch "$ENV_FILE"
  fi
fi

# Safe key=value write (avoids sed delimiter issues with secrets)
set_env() {
  local key="$1" val="$2" force="${3:-0}"
  local tmp
  tmp="$(mktemp)"
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    local cur
    cur="$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2-)"
    if [[ "$force" == "1" || -z "$cur" || "$key" == AZOM_* || "$key" == DASHBOARD_HOST || "$key" == DASHBOARD_PORT ]]; then
      awk -v k="$key" -v v="$val" '
        BEGIN { FS=OFS="=" }
        $1==k { print k "=" v; next }
        { print }
      ' "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE"
    else
      rm -f "$tmp"
    fi
  else
    echo "${key}=${val}" >> "$ENV_FILE"
    rm -f "$tmp"
  fi
}

set_env AZOM_USE_MOCK "$USE_MOCK"
set_env AZOM_CONFIG_DIR "${APP_DIR}/config"
set_env AZOM_DATA_DIR "$DATA_DIR"
set_env DASHBOARD_HOST "127.0.0.1"
set_env DASHBOARD_PORT "8080"
set_env DASHBOARD_USER "jonatan"

# Auto-generate dashboard passwords if empty (written once)
ensure_secret() {
  local key="$1"
  local cur
  cur="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  if [[ -z "$cur" ]]; then
    local external="${!key:-}"
    if [[ -n "$external" ]]; then
      set_env "$key" "$external" 1
      echo "$external"
    else
      local gen
      gen="$(gen_password)"
      set_env "$key" "$gen" 1
      echo "$gen"
    fi
  else
    echo "$cur"
  fi
}

DASH_PW="$(ensure_secret DASHBOARD_PASSWORD)"
OSCAR_PW="$(ensure_secret DASHBOARD_OSCAR_PASSWORD)"

# Optional secrets from installer environment (overwrite when provided)
for KEY in TELEGRAM_BOT_TOKEN WOO_CONSUMER_KEY WOO_CONSUMER_SECRET \
           OPENROUTER_API_KEY MAIL_PASSWORD SMTP_PASSWORD \
           MAIL_USERNAME MAIL_FROM WOO_BASE_URL; do
  if [[ -n "${!KEY:-}" ]]; then
    set_env "$KEY" "${!KEY}" 1
  fi
done

chown "${APP_USER}:${APP_USER}" "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok ".env ready at ${ENV_FILE}"

# Save generated credentials for operator (root only)
CREDS_FILE="/root/azom-install-credentials.txt"
{
  echo "# Generated by install-ubuntu26.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# Restrict: chmod 600 $CREDS_FILE"
  echo "APP_DIR=${APP_DIR}"
  echo "DASHBOARD_URL=http://127.0.0.1:8080"
  echo "DASHBOARD_USER=jonatan"
  echo "DASHBOARD_PASSWORD=${DASH_PW}"
  echo "DASHBOARD_OSCAR_USER=oscar"
  echo "DASHBOARD_OSCAR_PASSWORD=${OSCAR_PW}"
  echo "AZOM_USE_MOCK=${USE_MOCK}"
} > "$CREDS_FILE"
chmod 600 "$CREDS_FILE"
ok "Credentials written to ${CREDS_FILE}"

# ---------------------------------------------------------------------------
# 7) systemd units
# ---------------------------------------------------------------------------
log "Installing systemd units"
UNIT_SRC="${APP_DIR}/infrastructure/systemd"
if [[ ! -d "$UNIT_SRC" ]]; then
  die "Missing ${UNIT_SRC}"
fi

for unit in azom-dashboard.service azom-bot.service \
            azom-daily-brief.service azom-daily-brief.timer; do
  [[ -f "${UNIT_SRC}/${unit}" ]] || die "Missing unit ${unit}"
  cp "${UNIT_SRC}/${unit}" /etc/systemd/system/
done

# Rewrite paths / user for non-defaults
for f in /etc/systemd/system/azom-*.service /etc/systemd/system/azom-*.timer; do
  [[ -f "$f" ]] || continue
  sed -i "s|/opt/azom-agent|${APP_DIR}|g" "$f"
  sed -i "s|/var/lib/azom|${DATA_DIR}|g" "$f"
  sed -i "s|/var/log/azom|${LOG_DIR}|g" "$f"
  sed -i "s|User=azom|User=${APP_USER}|g" "$f"
  sed -i "s|Group=azom|Group=${APP_USER}|g" "$f"
done

# Ensure bin scripts executable in units
chmod +x "${APP_DIR}/bin/"*.sh

systemctl daemon-reload
ok "systemd units installed"

# ---------------------------------------------------------------------------
# 8) Firewall
# ---------------------------------------------------------------------------
if [[ "$SKIP_UFW" != "1" ]]; then
  log "Configuring UFW"
  ufw --force reset >/dev/null 2>&1 || true
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow "${SSH_PORT}/tcp" comment 'SSH'
  # Do not open 8080 publicly — reverse proxy or SSH tunnel
  ufw --force enable
  ok "UFW enabled (SSH :${SSH_PORT} only)"
else
  log "Skipping UFW"
fi

# ---------------------------------------------------------------------------
# 9) Hardening extras
# ---------------------------------------------------------------------------
log "fail2ban + unattended-upgrades + logrotate"
systemctl enable --now fail2ban 2>/dev/null || warn "fail2ban not started"
dpkg-reconfigure -f noninteractive unattended-upgrades 2>/dev/null || true

cat > /etc/logrotate.d/azom <<EOF
${LOG_DIR}/*.log {
  weekly
  rotate 8
  compress
  missingok
  notifempty
  copytruncate
  su ${APP_USER} ${APP_USER}
}
EOF

# ---------------------------------------------------------------------------
# 10) Smoke tests
# ---------------------------------------------------------------------------
if [[ "$RUN_SMOKE" == "1" ]]; then
  log "Running smoke tests"
  sudo -u "$APP_USER" bash <<EOF
set -euo pipefail
export PYTHONPATH="${APP_DIR}/skills"
export AZOM_CONFIG_DIR="${APP_DIR}/config"
export AZOM_DATA_DIR="${DATA_DIR}"
export AZOM_USE_MOCK=1
cd "${APP_DIR}"
.venv/bin/python -m ecom_ops --mock order-status --order-id 1001 --status completed >/dev/null
.venv/bin/python -m ecom_ops support --message "Order 1001 status?" >/dev/null
.venv/bin/python -m ecom_ops --mock ssh --command uptime >/dev/null
.venv/bin/python -m ecom_ops --mock mail fetch >/dev/null
.venv/bin/python -m ecom_ops --help >/dev/null
EOF
  ok "Smoke tests passed"
fi

# ---------------------------------------------------------------------------
# 11) Start services
# ---------------------------------------------------------------------------
if [[ "$START_SERVICES" == "1" ]]; then
  log "Enabling and starting services"
  systemctl enable azom-dashboard.service
  systemctl enable azom-daily-brief.timer
  systemctl restart azom-dashboard.service
  systemctl restart azom-daily-brief.timer

  # Bot only if token present
  BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)"
  if [[ -n "$BOT_TOKEN" ]]; then
    systemctl enable azom-bot.service
    systemctl restart azom-bot.service
    ok "azom-bot started"
  else
    systemctl disable azom-bot.service 2>/dev/null || true
    warn "TELEGRAM_BOT_TOKEN empty — bot not started (set token, then: systemctl enable --now azom-bot)"
  fi

  sleep 2
  if systemctl is-active --quiet azom-dashboard.service; then
    ok "azom-dashboard is active"
  else
    warn "azom-dashboard not active — check: journalctl -u azom-dashboard -n 50"
  fi

  # Health probe
  if curl -sf "http://127.0.0.1:8080/health" >/dev/null 2>&1; then
    ok "Dashboard health endpoint OK"
  else
    warn "Dashboard /health not responding yet (may still be starting)"
  fi
else
  log "Skipping service start (--no-start)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================================="
echo "  Azom Ops Agent install complete"
echo "============================================================================="
echo "  OS:           ${OS_ID} ${OS_VERSION}"
echo "  App dir:      ${APP_DIR}"
echo "  Data dir:     ${DATA_DIR}"
echo "  User:         ${APP_USER}"
echo "  Mock mode:    ${USE_MOCK}"
echo "  Dashboard:    http://127.0.0.1:8080  (user: jonatan)"
echo "  Credentials:  ${CREDS_FILE}"
echo ""
echo "  Next:"
echo "    1. Review secrets:  nano ${ENV_FILE}"
echo "    2. Put nginx/Caddy in front for public HTTPS (do not open :8080 in UFW)"
echo "    3. Status:          systemctl status azom-dashboard"
echo "    4. Logs:            journalctl -u azom-dashboard -f"
echo "    5. CLI:             sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/python -m ecom_ops --help"
echo ""
echo "  Re-run this script anytime — it is idempotent."
echo "============================================================================="
