#!/bin/bash
# Daily SQLite backup for tatami-bot
#
# Usage: scripts/backup_db.sh
# Cron:  0 3 * * * /home/amit/tatami-bot/scripts/backup_db.sh
#
# Keeps 14 days of backups. Uses SQLite .backup for consistency.

set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB_PATH="${BASE_DIR}/data/tatami.db"
BACKUP_DIR="${BASE_DIR}/data/backups"
DATE=$(date +%Y%m%d)
KEEP_DAYS=14

if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: Database not found at $DB_PATH"
    exit 1
fi

mkdir -p "$BACKUP_DIR"

BACKUP_FILE="${BACKUP_DIR}/tatami-${DATE}.db"

# Use SQLite .backup command for a consistent snapshot
sqlite3 "$DB_PATH" ".backup '$BACKUP_FILE'"

# Compress the backup
gzip -f "$BACKUP_FILE"

echo "Backup created: ${BACKUP_FILE}.gz ($(du -h "${BACKUP_FILE}.gz" | cut -f1))"

# Remove backups older than KEEP_DAYS
find "$BACKUP_DIR" -name "tatami-*.db.gz" -mtime +${KEEP_DAYS} -delete 2>/dev/null || true

# List current backups
echo "Current backups:"
ls -lh "$BACKUP_DIR"/tatami-*.db.gz 2>/dev/null || echo "  (none)"
