#!/bin/sh
set -eu
status_dir="${BACKUP_ROOT:-/backups}/status"
now=$(date +%s)
timestamp_age() {
    [ -s "$1" ] || return 1
    value=$(cat "$1")
    case "$value" in *[!0-9]*|'') return 1 ;; esac
    [ "$value" -le "$now" ] || return 1
    [ "$((now - value))" -le "$2" ]
}
timestamp_age "$status_dir/heartbeat" 180 || exit 1
# A completed failure must be visible immediately, including a wrong repository
# password during initialisation. Successful jobs clear this marker under flock.
[ ! -e "$status_dir/last_failure" ] || exit 1
if [ -s "$status_dir/last_success" ]; then
    timestamp_age "$status_dir/last_success" "${BACKUP_MAX_AGE_SECONDS:-93600}"
else
    # Allow initialisation/first snapshot to complete, while still detecting a
    # scheduler that has never created a usable backup after the grace period.
    timestamp_age "$status_dir/scheduler_started" "${BACKUP_MAX_AGE_SECONDS:-93600}"
fi
