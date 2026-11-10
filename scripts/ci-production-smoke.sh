#!/bin/sh
# CI-only checks: use generated credentials and a disposable Compose project.
set -eu

if [ "${GITHUB_ACTIONS:-}" != true ]; then
    echo 'This script runs only in GitHub Actions, on disposable test data.' >&2
    exit 2
fi
mode=${1:-config}
case "$mode" in
    config|images) ;;
    *) echo 'Usage: ci-production-smoke.sh [config|images]' >&2; exit 2 ;;
esac

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
smoke_dir=$(mktemp -d "${RUNNER_TEMP:?}/product-ci-smoke.XXXXXX")
compose_project="product-ci-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:-1}"
containers_started=0

compose() {
    docker compose --project-name "$compose_project" \
        --env-file "$smoke_dir/.env.prod" -f "$smoke_dir/compose.prod.yaml" "$@"
}

cleanup() {
    result=$?
    trap - EXIT INT TERM
    if [ "$containers_started" -eq 1 ]; then
        if [ "$result" -ne 0 ]; then
            compose ps --all || true
            compose logs --no-color --tail=100 web worker backup || true
        fi
        compose down --volumes --remove-orphans --timeout=15 || true
    fi
    # Backup files were created by container UID 0. This is a generated directory
    # inside RUNNER_TEMP, never the checkout or a configured server backup path.
    if [ "$mode" = images ]; then
        sudo rm -rf -- "$smoke_dir"
    else
        rm -rf -- "$smoke_dir"
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$smoke_dir/scripts"
cp "$project_dir/compose.prod.yaml" "$project_dir/Caddyfile" \
    "$project_dir/.env.prod.example" "$smoke_dir/"
cp "$project_dir/scripts/setup-server.sh" "$project_dir/scripts/server.sh" \
    "$smoke_dir/scripts/"
sh "$smoke_dir/scripts/setup-server.sh" ci.example.org
compose config --quiet

if [ "$mode" = config ]; then
    docker run --rm --env APP_DOMAIN=ci.example.org \
        --mount "type=bind,source=$smoke_dir/Caddyfile,target=/etc/caddy/Caddyfile,readonly" \
        caddy:2.10.2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
    echo 'Production Compose configuration and Caddy validation passed.'
    exit 0
fi

# Only exact outputs of this CI run are accepted; never smoke-test a moving tag.
case "${APP_IMAGE:-}" in
    ghcr.io/kd170703/product@sha256:*) ;;
    *) echo 'APP_IMAGE must be the just-built application digest.' >&2; exit 2 ;;
esac
case "${BACKUP_IMAGE:-}" in
    ghcr.io/kd170703/product-backup@sha256:*) ;;
    *) echo 'BACKUP_IMAGE must be the just-built backup digest.' >&2; exit 2 ;;
esac
export APP_IMAGE BACKUP_IMAGE
compose pull db web worker backup

if docker run --rm --network none --entrypoint python "$APP_IMAGE" \
    -c 'import config.settings' > "$smoke_dir/missing-secret.log" 2>&1; then
    echo 'Application unexpectedly accepted a missing DJANGO_SECRET_KEY.' >&2
    exit 1
fi
python3 - "$smoke_dir/missing-secret.log" <<'PY'
from pathlib import Path
import sys

assert "Не задан DJANGO_SECRET_KEY" in Path(sys.argv[1]).read_text(), (
    "Application must fail specifically because DJANGO_SECRET_KEY is missing."
)
PY

docker run --rm --network none --read-only --entrypoint /bin/sh "$BACKUP_IMAGE" -ec '
    restic version
    case "$(pg_dump --version)" in
        "pg_dump (PostgreSQL) 17."*) ;;
        *) echo "Expected PostgreSQL 17 pg_dump." >&2; exit 1 ;;
    esac
    command -v flock
    test "$(TZ=Europe/Moscow date +%z)" = +0300
    for script in /usr/local/bin/backup-*.sh /usr/local/bin/restore-backup.sh; do
        sh -n "$script"
    done
'

containers_started=1
# No public ports, no certificate issuance: Caddy was validated in the check job.
compose up --detach --wait --wait-timeout=180 db web worker backup
compose exec -T web python manage.py shell <<'PY'
import json
from pathlib import Path
import urllib.request

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client

assert settings.REQUIRE_TWO_FACTOR
assert settings.SECURE_SSL_REDIRECT and settings.SESSION_COOKIE_SECURE
assert not settings.OPENAI_ENABLED
with urllib.request.urlopen("http://127.0.0.1:8000/health/", timeout=5) as response:
    assert json.load(response) == {"status": "ok"}
response = Client().get(
    "/accounts/login/", HTTP_HOST="ci.example.org", HTTP_X_FORWARDED_PROTO="https"
)
assert response.status_code == 200
get_user_model().objects.create_user(username="ci-backup-probe")
probe = Path(settings.MEDIA_ROOT) / "ci-smoke.txt"
probe.write_text("ci-smoke-success", encoding="utf-8")
probe.chmod(0o600)
PY

# The scheduler must create its first successful backup without a browser.
attempt=0
while ! compose exec -T backup test -s /backups/status/last_success; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        echo 'The background scheduler did not create its initial backup.' >&2
        exit 1
    fi
    sleep 2
done

# Create a later snapshot containing the synthetic DB row and a mode-0600 file.
attempt=0
while :; do
    if compose exec -T backup /usr/local/bin/backup-now.sh; then
        break
    else
        result=$?
        attempt=$((attempt + 1))
        if [ "$result" -ne 75 ] || [ "$attempt" -ge 30 ]; then
            echo 'Manual backup failed after scheduler startup.' >&2
            exit 1
        fi
        sleep 2
    fi
done
compose exec -T backup /usr/local/bin/backup-entrypoint.sh restic check --read-data
restored_media=$(compose exec -T backup /usr/local/bin/backup-entrypoint.sh \
    restic dump latest /media/ci-smoke.txt)
test "$restored_media" = ci-smoke-success

# Restore only into a separate empty database in this disposable CI project.
compose exec -T db sh -ec 'createdb -U "$POSTGRES_USER" ci_restore'
compose exec -T backup /usr/local/bin/backup-entrypoint.sh \
    restic dump latest /work/snapshot/database.dump | \
    compose exec -T db sh -ec 'pg_restore -U "$POSTGRES_USER" -d ci_restore --no-owner --no-acl --exit-on-error'
restored_count=$(
    printf "SELECT COUNT(*) FROM auth_user WHERE username = 'ci-backup-probe';\n" | \
        compose exec -T db sh -ec 'psql -U "$POSTGRES_USER" -d ci_restore -Atq'
)
test "$restored_count" = 1
for service in web worker backup; do
    container_id=$(compose ps --quiet "$service")
    state=$(docker inspect --format '{{.State.Running}} {{.RestartCount}}' "$container_id")
    test "$state" = 'true 0'
done
echo 'Production images passed health, authentication configuration and backup restore checks.'
