#!/bin/sh
set -eu

# Only the web process performs schema/bootstrap changes. The worker starts
# after web passes its health check, so it never races migrations.
if [ "${1:-}" = "gunicorn" ]; then
  python manage.py migrate --noinput
  python manage.py bootstrap
  python manage.py collectstatic --noinput
fi

exec "$@"
