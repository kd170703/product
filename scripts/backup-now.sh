#!/bin/sh
set -eu
. "${BACKUP_SCRIPT_DIR:-/usr/local/bin}/backup-common.sh"

backup_prepare
# The host directory is shared by scheduler and one-off containers. flock releases
# automatically even when a process/container is killed; no stale PID guessing.
exec 9> "$BACKUP_ROOT/.backup.lock"
if ! flock -n 9; then
    backup_log 'Another backup or restore is already running; nothing was changed.'
    exit 75
fi

snapshot_dir="$BACKUP_WORK/snapshot"
backup_ok=0
cleanup() {
    result=$?
    trap - EXIT
    rm -rf "$snapshot_dir"
    if [ "$backup_ok" -eq 0 ]; then
        backup_timestamp "$BACKUP_ROOT/status/last_failure"
        backup_log 'ERROR: backup did not complete. The last-success timestamp was not advanced; inspect the logs.' >&2
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

backup_database_credentials
backup_repository
mkdir -p "$snapshot_dir"
backup_timestamp "$BACKUP_ROOT/status/last_attempt"
backup_log 'Starting PostgreSQL snapshot and encrypted media backup.'

# PostgreSQL provides a consistent MVCC snapshot. Source publications and released
# reports are immutable files, written before their database reference commits.
# Dump first, then copy media: all files referenced by that database snapshot exist.
pg_dump --format=custom --no-owner --no-acl --file="$snapshot_dir/database.dump"
{
    printf 'backup_format=1\npostgres_major=17\ndatabase_format=pg_dump_custom\n'
    printf 'created_at_utc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'database=%s\n' "$PGDATABASE"
    printf 'media_policy=immutable_files_dump_first\n'
} > "$snapshot_dir/manifest.txt"

restic backup "$snapshot_dir" /media --host "$BACKUP_HOST" --tag daily --quiet
# Verify metadata and read a rotating random sample of encrypted data each day.
# A complete read check is documented for periodic restore drills.
restic check --read-data-subset=5% --quiet
restic forget --host "$BACKUP_HOST" --tag daily \
    --keep-daily "${BACKUP_KEEP_DAILY:-30}" \
    --keep-weekly "${BACKUP_KEEP_WEEKLY:-4}" \
    --keep-monthly "${BACKUP_KEEP_MONTHLY:-12}" --prune --quiet

backup_timestamp "$BACKUP_ROOT/status/last_success"
rm -f "$BACKUP_ROOT/status/last_failure"
backup_ok=1
backup_log 'Backup completed and verified. Retention policy applied.'
