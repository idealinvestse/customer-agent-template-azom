#!/usr/bin/env bash
# Bootstrap Azom agent on Ubuntu 24.04 LTS (Hetzner VPS).
# Run as root on a fresh server: sudo bash bin/bootstrap-ubuntu24.sh
set -euo pipefail

APP_USER="${AZOM_USER:-azom}"
APP_DIR="${AZOM_APP_DIR:-/opt/azom-agent}"
REPO_URL="${AZOM_REPO_URL:-}"
SSH_PORT="${AZOM_SSH_PORT:-22}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

if [[ ! -f /etc/os-release ]] || ! grep -q 'VERSION_ID="24.04"' /etc/os-release; then
  echo "WARNING: Expected Ubuntu 24.04. Continuing anyway."
fi

export DEBIAN_FRONTEND=noninteractive

echo "==> System packages"
apt-get update -qq
apt-get install -y -qq \
  ca-certificates curl gnupg lsb-release git \
  python3 python3-pip python3-venv python3-dev \
  openssh-client ufw fail2ban unattended-upgrades \
  jq logrotate

echo "==> Docker Engine (Ubuntu 24 / official repo)"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

echo "==> App user: ${APP_USER}"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash --home-dir "/home/${APP_USER}" "$APP_USER"
fi
usermod -aG docker "$APP_USER" || true

echo "==> App directory: ${APP_DIR}"
mkdir -p "$APP_DIR" /var/lib/azom /var/log/azom
chown -R "${APP_USER}:${APP_USER}" "$APP_DIR" /var/lib/azom /var/log/azom

if [[ -n "$REPO_URL" && ! -d "${APP_DIR}/.git" ]]; then
  echo "==> Clone repo ${REPO_URL}"
  sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
elif [[ ! -f "${APP_DIR}/pyproject.toml" ]]; then
  # Copy from current checkout if bootstrap is run from repo root
  SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [[ -f "${SCRIPT_ROOT}/pyproject.toml" ]]; then
    echo "==> Copying project from ${SCRIPT_ROOT}"
    rsync -a --exclude .git --exclude .venv --exclude .azom-data \
      "${SCRIPT_ROOT}/" "${APP_DIR}/"
    chown -R "${APP_USER}:${APP_USER}" "$APP_DIR"
  fi
fi

if [[ -f "${APP_DIR}/pyproject.toml" ]]; then
  echo "==> Python venv + install"
  sudo -u "$APP_USER" bash -c "
    cd '$APP_DIR'
    python3 -m venv .venv
    .venv/bin/pip install -U pip wheel
    .venv/bin/pip install -r requirements.txt
    .venv/bin/pip install -e .
  "
fi

if [[ ! -f "${APP_DIR}/.env" && -f "${APP_DIR}/.env.example" ]]; then
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
  # Production defaults
  sed -i 's/^AZOM_USE_MOCK=.*/AZOM_USE_MOCK=0/' "${APP_DIR}/.env" || true
  {
    echo ""
    echo "# Set by bootstrap-ubuntu24.sh"
    echo "AZOM_CONFIG_DIR=${APP_DIR}/config"
    echo "AZOM_DATA_DIR=/var/lib/azom"
    echo "DASHBOARD_HOST=127.0.0.1"
    echo "DASHBOARD_PORT=8080"
  } >> "${APP_DIR}/.env"
  echo "Created ${APP_DIR}/.env — fill in secrets before start."
fi

echo "==> Install systemd units"
UNIT_SRC="${APP_DIR}/infrastructure/systemd"
if [[ -d "$UNIT_SRC" ]]; then
  cp "${UNIT_SRC}/azom-dashboard.service" /etc/systemd/system/
  cp "${UNIT_SRC}/azom-bot.service" /etc/systemd/system/
  cp "${UNIT_SRC}/azom-daily-brief.service" /etc/systemd/system/
  cp "${UNIT_SRC}/azom-daily-brief.timer" /etc/systemd/system/
  # Rewrite paths if non-default
  sed -i "s|/opt/azom-agent|${APP_DIR}|g" /etc/systemd/system/azom-*.service
  sed -i "s|User=azom|User=${APP_USER}|g" /etc/systemd/system/azom-*.service
  sed -i "s|Group=azom|Group=${APP_USER}|g" /etc/systemd/system/azom-*.service
  systemctl daemon-reload
fi

echo "==> UFW firewall"
ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}/tcp" comment 'SSH'
# Dashboard only via localhost + optional reverse proxy (nginx) — do NOT open 8080 publicly by default
ufw --force enable

echo "==> Unattended upgrades"
dpkg-reconfigure -f noninteractive unattended-upgrades || true

echo "==> Logrotate"
cat > /etc/logrotate.d/azom <<'EOF'
/var/log/azom/*.log {
  weekly
  rotate 8
  compress
  missingok
  notifempty
  copytruncate
}
EOF

echo ""
echo "Bootstrap complete."
echo "Next steps:"
echo "  1. Edit secrets:  nano ${APP_DIR}/.env"
echo "  2. Enable services:"
echo "       systemctl enable --now azom-dashboard.service"
echo "       systemctl enable --now azom-bot.service        # needs TELEGRAM_BOT_TOKEN"
echo "       systemctl enable --now azom-daily-brief.timer"
echo "  3. Or use Docker: cd ${APP_DIR} && docker compose -f infrastructure/docker-compose.prod.yml up -d --build"
echo "  4. Smoke: sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/python -m ecom_ops --help"
echo ""
echo "Recommended Hetzner size for single-tenant pilot: CX22 / CPX21 (2 vCPU, 4 GB RAM)."
