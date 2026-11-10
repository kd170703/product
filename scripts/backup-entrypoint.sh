#!/bin/sh
set -eu
. "${BACKUP_SCRIPT_DIR:-/usr/local/bin}/backup-common.sh"
script_dir=${BACKUP_SCRIPT_DIR:-/usr/local/bin}

case "${1:-scheduler}" in
    backup-now) exec "$script_dir/backup-now.sh" ;;
    restore) shift; exec "$script_dir/restore-backup.sh" "$@" ;;
    healthcheck) exec "$script_dir/backup-healthcheck.sh" ;;
    restic)
        shift
        backup_prepare
        exec restic "$@"
        ;;
    scheduler) ;;
    *) exec "$@" ;;
esac

backup_prepare
backup_time=${BACKUP_TIME:-03:00}
case "$backup_time" in
    [0-2][0-9]:[0-5][0-9]) ;;
    *) backup_log 'ERROR: BACKUP_TIME must use HH:MM.' >&2; exit 1 ;;
esac
backup_hour=${backup_time%:*}
if [ "$backup_hour" -ge 24 ]; then
    backup_log 'ERROR: BACKUP_TIME hour must be between 00 and 23.' >&2
    exit 1
fi
backup_timestamp "$BACKUP_ROOT/status/scheduler_started"
backup_log "Daily backup scheduler started: $backup_time (${TZ:-system timezone})."
next_retry=0
job_pid=''
stop_scheduler() {
    trap - INT TERM
    if [ -n "$job_pid" ]; then
        kill -TERM "$job_pid" 2>/dev/null || true
        wait "$job_pid" 2>/dev/null || true
    fi
    exit 0
}
trap stop_scheduler INT TERM

while :; do
    backup_timestamp "$BACKUP_ROOT/status/heartbeat"
    now=$(date +%s)
    today=$(date '+%Y-%m-%d')
    current_time=$(date '+%H:%M')
    last_day=''
    if [ -s "$BACKUP_ROOT/status/last_success" ]; then
        last_success=$(cat "$BACKUP_ROOT/status/last_success")
        last_day=$(date -d "@$last_success" '+%Y-%m-%d')
    fi
    # First installation runs immediately. Following runs follow Moscow calendar
    # days rather than sleeping 24 hours (which drifts after a lengthy backup).
    due=0
    if [ -z "$last_day" ]; then
        due=1
    elif [ "$last_day" != "$today" ] && [ "$current_time" \> "$backup_time" -o "$current_time" = "$backup_time" ]; then
        due=1
    fi
    if [ "$due" -eq 1 ] && [ "$now" -ge "$next_retry" ]; then
        "$script_dir/backup-now.sh" &
        job_pid=$!
        while kill -0 "$job_pid" 2>/dev/null; do
            backup_timestamp "$BACKUP_ROOT/status/heartbeat"
            sleep 30 &
            wait "$!" || true
        done
        if wait "$job_pid"; then
            next_retry=0
        else
            next_retry=$(($(date +%s) + ${BACKUP_RETRY_SECONDS:-900}))
            backup_log 'The backup will be retried after the configured retry interval.' >&2
        fi
        job_pid=''
    fi
    sleep 30 &
    wait "$!" || true
done
