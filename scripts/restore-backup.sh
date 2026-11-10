#!/bin/sh
set -eu
. "${BACKUP_SCRIPT_DIR:-/usr/local/bin}/backup-common.sh"

snapshot=${1:-}
target=${2:-/restore}
if [ -z "$snapshot" ] || [ "$snapshot" = latest ]; then
    backup_log 'ERROR: provide an explicit snapshot ID (not latest), obtained with restic snapshots.' >&2
    exit 2
fi
# This command intentionally never connects to PostgreSQL or writes live media.
# Mount an empty directory at /restore; review the staged contents before use.
if [ "$target" != /restore ]; then
    backup_log 'ERROR: the restore staging target must be /restore.' >&2
    exit 2
fi
if [ ! -d "$target" ] || [ -n "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    backup_log 'ERROR: mount an existing, empty directory at /restore.' >&2
    exit 2
fi
backup_prepare
exec 9> "$BACKUP_ROOT/.backup.lock"
if ! flock -n 9; then
    backup_log 'Another backup or restore is already running; nothing was changed.'
    exit 75
fi
restic restore "$snapshot" --target "$target" --verify
backup_log 'Snapshot restored to staging only. Live database and media were not changed.'
