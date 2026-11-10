#!/bin/sh
# Shared functions; never enable shell tracing here: database credentials are loaded.
set -eu
umask 077

BACKUP_ROOT=${BACKUP_ROOT:-/backups}
BACKUP_WORK=${BACKUP_WORK:-/work}
BACKUP_HOST=${BACKUP_HOST:-financial-culture-monitoring}
RESTIC_REPOSITORY=${RESTIC_REPOSITORY:-$BACKUP_ROOT/restic}
RESTIC_CACHE_DIR=${RESTIC_CACHE_DIR:-$BACKUP_WORK/restic-cache}
export RESTIC_REPOSITORY RESTIC_CACHE_DIR

backup_log() {
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

backup_timestamp() {
    # Rename within one filesystem, so readers never see a partial timestamp.
    stamp_path=$1
    printf '%s\n' "$(date +%s)" > "$stamp_path.tmp.$$"
    mv -f "$stamp_path.tmp.$$" "$stamp_path"
}

backup_prepare() {
    mkdir -p "$BACKUP_ROOT/status" "$BACKUP_WORK"
    : "${RESTIC_PASSWORD_FILE:?Set RESTIC_PASSWORD_FILE to a mounted secret}"
    if [ ! -s "$RESTIC_PASSWORD_FILE" ]; then
        backup_log 'ERROR: the Restic password file is missing or empty.' >&2
        exit 1
    fi
    export RESTIC_PASSWORD_FILE
}

backup_database_credentials() {
    : "${PGHOST:?Set PGHOST}"
    : "${PGDATABASE:?Set PGDATABASE}"
    : "${PGUSER:?Set PGUSER}"
    : "${PGPASSWORD_FILE:?Set PGPASSWORD_FILE to a mounted secret}"
    if [ ! -s "$PGPASSWORD_FILE" ]; then
        backup_log 'ERROR: the database password file is missing or empty.' >&2
        exit 1
    fi
    PGPASSWORD=$(cat "$PGPASSWORD_FILE")
    export PGPASSWORD
    export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-10}
}

backup_repository() {
    if restic cat config >/dev/null 2>&1; then
        return
    fi
    # An unavailable or unreadable existing repository must never be reinitialised.
    # Remote storage is supported only after explicit administrator initialisation.
    case "$RESTIC_REPOSITORY" in
        /*)
            mkdir -p "$RESTIC_REPOSITORY"
            if [ -n "$(find "$RESTIC_REPOSITORY" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
                backup_log 'ERROR: existing repository cannot be opened; check the password, storage and logs.' >&2
                exit 1
            fi
            backup_log 'Initialising an encrypted, empty local backup repository.'
            restic init --quiet
            ;;
        *)
            backup_log 'ERROR: initialise and verify the external repository explicitly before starting the scheduler.' >&2
            exit 1
            ;;
    esac
}
