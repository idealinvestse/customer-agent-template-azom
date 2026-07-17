#!/usr/bin/env bash
# =============================================================================
# Azom Ops Agent — backup /var/lib/azom (cases.db + telemetry + oauth + escalations)
#
# Creates a timestamped SQLite .backup of cases.db (safe, online) plus a tar
# of the full data dir, then optionally rsyncs to an off-box destination.
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

# --- 1) Online SQLite backup of cases.db (safe under concurrent writes) ---
CASES_DB="${DATA_DIR}/cases.db"
if [[ -f "$CASES_DB" ]]; then
  log "Backing up cases.db (online .backup)"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$CASES_DB" ".backup '${STAMP_DIR}/cases.db'"
  else
    warn "sqlite3 not installed — falling back to cp (may be inconsistent under writes)"
    cp "$CASES_DB" "${STAMP_DIR}/cases.db"
  fi
else
  warn "cases.db not found — skipping"
fi

# --- 2) Tar the full data dir (excluding backups/ itself + caches) ---
log "Creating tarball of ${DATA_DIR}"
TARBALL="${STAMP_DIR}/azom-data.tar.gz"
tar -czf "$TARBALL" \
  --exclude="${BACKUP_DIR}" \
  --exclude="*/__pycache__" \
  --exclude="*/.pytest_cache" \
  -C "$(dirname "$DATA_DIR")" \
  "$(basename "$DATA_DIR")"

# --- 3) Optional off-box sync ---
if [[ -n "$DEST" ]]; then
  log "rsync to ${DEST}"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "${BACKUP_DIR}/" "$DEST"
  else
    warn "rsync not installed — skipping off-box sync"
  fi
fi

# --- 4) Retention: keep last N backup dirs ---
log "Pruning backups older than ${KEEP} days"
find "$BACKUP_DIR" -maxdepth 1 -type d -name '20*' -mtime +${KEEP} -exec rm -rf {} + 2>/dev/null || true

# --- 5) Manifest ---
{
  echo "backup_ts=${TS}"
  echo "data_dir=${DATA_DIR}"
  echo "backup_dir=${STAMP_DIR}"
  echo "cases_db_bytes=$(stat -c%s "${STAMP_DIR}/cases.db" 2>/dev/null || echo 0)"
  echo "tarball_bytes=$(stat -c%s "$TARBALL" 2>/dev/null || echo 0)"
  echo "dest=${DEST:-local}"
} > "${STAMP_DIR}/manifest.txt"

ok_size="$(du -sh "$STAMP_DIR" | cut -f1)"
log "Backup complete: ${STAMP_DIR} (${ok_size})"
