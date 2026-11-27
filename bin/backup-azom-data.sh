#!/usr/bin/env bash
# =============================================================================
# Azom Ops Agent — backup /var/lib/azom (cases.db + telemetry + oauth + escalations)
#
# Creates timestamped online SQLite backups of cases.db and the Telegram job
# ledger plus an agent-state tar without runtime databases or installed libraries.
# Optionally rsyncs the resulting backup directory to an off-box destination.
#
# Usage (as root or the azom user):
#   bash bin/backup-azom-data.sh
#   bash bin/backup-azom-data.sh --dest rsync://user@storage-box/azom-backups/
#   bash bin/backup-azom-data.sh --keep 14        # retain last 14 backups locally
#
# Env overrides:
#   AZOM_DATA_DIR (default /var/lib/azom)
#   AZOM_BACKUP_DIR (default /var/lib/azom/backups)
#   AZOM_BACKUP_DEST (rsync destination; empty = local only)
# =============================================================================
set -euo pipefail
umask 077

DATA_DIR="${AZOM_DATA_DIR:-/var/lib/azom}"
BACKUP_DIR="${AZOM_BACKUP_DIR:-${DATA_DIR}/backups}"
DEST="${AZOM_BACKUP_DEST:-}"
KEEP=30

log()  { echo -e "\n\033[1;32m==>\033[0m $*"; }
warn() { echo -e "\033[1;33mWARN:\033[0m $*" >&2; }
die()  { echo -e "\033[1;31mERROR:\033[0m $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest) DEST="$2"; shift 2 ;;
    --keep) KEEP="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \?//'
      exit 0 ;;
    *) die "Unknown arg: $1 (try --help)" ;;
  esac
done

[[ -d "$DATA_DIR" ]] || die "AZOM_DATA_DIR not found: $DATA_DIR"
mkdir -p "$BACKUP_DIR"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
STAMP_DIR="${BACKUP_DIR}/${TS}"
mkdir -p "$STAMP_DIR"

backup_sqlite() {
  local source="$1" target="$2"
  python3 - "$source" "$target" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1], timeout=10)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target, pages=256, sleep=0.1)
finally:
    target.close()
    source.close()
PY
}

# SQLite's online backup API keeps each live database consistent under WAL writes.
for database in cases telegram_jobs research_jobs messenger_jobs messenger_proposals; do
  case "$database" in
    cases)
      SOURCE_DB="${DATA_DIR}/cases.db"
      TARGET_DB="${STAMP_DIR}/cases.db"
      ;;
    telegram_jobs)
      SOURCE_DB="${DATA_DIR}/telegram_jobs.sqlite3"
      TARGET_DB="${STAMP_DIR}/telegram_jobs.sqlite3"
      ;;
    research_jobs)
      SOURCE_DB="${DATA_DIR}/research_jobs.sqlite3"
      TARGET_DB="${STAMP_DIR}/research_jobs.sqlite3"
      ;;
    messenger_jobs)
      SOURCE_DB="${DATA_DIR}/messenger_jobs.sqlite3"
      TARGET_DB="${STAMP_DIR}/messenger_jobs.sqlite3"
      ;;
    messenger_proposals)
      SOURCE_DB="${DATA_DIR}/messenger_proposals.sqlite3"
      TARGET_DB="${STAMP_DIR}/messenger_proposals.sqlite3"
      ;;
  esac
  if [[ -f "$SOURCE_DB" ]]; then
    log "Creating online SQLite backup of ${database}"
    backup_sqlite "$SOURCE_DB" "$TARGET_DB"
  fi
done

# --- 2) Tar agent state only; omit runtime databases copied above and libraries ---
log "Creating tarball of Azom agent state under ${DATA_DIR}"
TARBALL="${STAMP_DIR}/azom-data.tar.gz"
# model_oauth holds ChatGPT refresh tokens.
# ssh-keys holds per-site private keys for the WordPress file transport.
# wp_file_payloads holds theme and plugin review copies outside the plan record.
# wp_workspaces holds copied source, diffs, and check artifacts; it is excluded.
# wp_review_plans.json metadata is included. A restored plan without its workspace
# is marked not_resumable by the application and cannot resume checks or staging.
# research_artifacts holds rendered per-job briefs/sources; excluded from the tarball
# because research_jobs.sqlite3 is backed up separately and artifacts can grow large.
# On restore without research_artifacts, get_brief() reports artifacts unavailable.
# Take a separate encrypted copy if Oscar needs an off-box restore.
tar -czf "$TARBALL" \
  --exclude="${BACKUP_DIR}" \
  --exclude="$(basename "$BACKUP_DIR")" \
  --exclude="*/$(basename "$BACKUP_DIR")" \
  --exclude="*/$(basename "$BACKUP_DIR")/*" \
  --exclude="*/__pycache__" \
  --exclude="*/.pytest_cache" \
  --exclude="*/.mypy_cache" \
  --exclude="*/.ruff_cache" \
  --exclude="*/.cache" \
  --exclude="*/.cache/*" \
  --exclude="*/node_modules" \
  --exclude="*/node_modules/*" \
  --exclude="*/.venv" \
  --exclude="*/.venv/*" \
  --exclude="*/venv" \
  --exclude="*/venv/*" \
  --exclude="*/site-packages" \
  --exclude="*/site-packages/*" \
  --exclude="*/cases.db" \
  --exclude="*/cases.db-wal" \
  --exclude="*/cases.db-shm" \
  --exclude="*/telegram_jobs.sqlite3" \
  --exclude="*/telegram_jobs.sqlite3-wal" \
  --exclude="*/telegram_jobs.sqlite3-shm" \
  --exclude="*/research_jobs.sqlite3" \
  --exclude="*/research_jobs.sqlite3-wal" \
  --exclude="*/research_jobs.sqlite3-shm" \
  --exclude="*/messenger_jobs.sqlite3" \
  --exclude="*/messenger_jobs.sqlite3-wal" \
  --exclude="*/messenger_jobs.sqlite3-shm" \
  --exclude="*/messenger_proposals.sqlite3" \
  --exclude="*/messenger_proposals.sqlite3-wal" \
  --exclude="*/messenger_proposals.sqlite3-shm" \
  --exclude="*/web_search_cache.sqlite3*" \
  --exclude="*/model_oauth" \
  --exclude="*/model_oauth/*" \
  --exclude="*/ssh-keys" \
  --exclude="*/ssh-keys/*" \
  --exclude="*/wp_file_payloads" \
  --exclude="*/wp_file_payloads/*" \
  --exclude="*/wp_workspaces" \
  --exclude="*/wp_workspaces/*" \
  --exclude="*/research_artifacts*" \
  --exclude="*/wp_ssh.lock" \
  --exclude="*/search_router_health.lock" \
  -C "$(dirname "$DATA_DIR")" \
  "$(basename "$DATA_DIR")"

# --- 3) Retention: drop backup dirs older than KEEP days ---
log "Pruning backups older than ${KEEP} days"
find "$BACKUP_DIR" -maxdepth 1 -type d -name '20*' -mtime +${KEEP} -exec rm -rf {} + 2>/dev/null || true

# --- 4) Manifest ---
{
  echo "backup_ts=${TS}"
  echo "data_dir=${DATA_DIR}"
  echo "backup_dir=${STAMP_DIR}"
  echo "cases_db_bytes=$(stat -c%s "${STAMP_DIR}/cases.db" 2>/dev/null || echo 0)"
  echo "telegram_jobs_db_bytes=$(stat -c%s "${STAMP_DIR}/telegram_jobs.sqlite3" 2>/dev/null || echo 0)"
  echo "research_jobs_db_bytes=$(stat -c%s "${STAMP_DIR}/research_jobs.sqlite3" 2>/dev/null || echo 0)"
  echo "messenger_jobs_db_bytes=$(stat -c%s "${STAMP_DIR}/messenger_jobs.sqlite3" 2>/dev/null || echo 0)"
  echo "messenger_proposals_db_bytes=$(stat -c%s "${STAMP_DIR}/messenger_proposals.sqlite3" 2>/dev/null || echo 0)"
  echo "tarball_bytes=$(stat -c%s "$TARBALL" 2>/dev/null || echo 0)"
  echo "dest=${DEST:-local}"
} > "${STAMP_DIR}/manifest.txt"

# --- 5) Optional off-box sync ---
if [[ -n "$DEST" ]]; then
  log "rsync to ${DEST}"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "${BACKUP_DIR}/" "$DEST"
  else
    warn "rsync not installed — skipping off-box sync"
  fi
fi

ok_size="$(du -sh "$STAMP_DIR" | cut -f1)"
log "Backup complete: ${STAMP_DIR} (${ok_size})"
