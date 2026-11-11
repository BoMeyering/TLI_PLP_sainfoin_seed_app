#!/bin/sh
set -e

python manage.py migrate --noinput

# Creates a superuser from DJANGO_SUPERUSER_* env vars on first boot, if set;
# a no-op on every later restart once that account already exists.
python manage.py ensure_superuser

# supercronic runs the scheduled cleanup commands (see docker/crontab) for the
# life of the container; gunicorn serves the app. Backgrounding supercronic and
# exec'ing gunicorn keeps gunicorn as PID 1, so it gets `docker stop`'s SIGTERM
# directly and shuts down cleanly instead of waiting out the grace period.
supercronic /app/docker/crontab &

# Long-running SSE responses (image processing progress, Drive upload
# progress) hold a connection open for as long as that job takes. Gunicorn's
# default `sync` worker can't survive that: it only checks in with the
# arbiter between requests, so a single request running past --timeout gets
# SIGKILL'd mid-stream NO MATTER HOW HIGH --timeout is set — confirmed by
# testing a sync worker against a slow streaming response, which was killed
# partway through even with a generous timeout. `gthread` workers check in
# from a separate thread, so a worker actively streaming a slow response
# still gets reaped correctly if it's genuinely hung, without capping how
# long a legitimate large job can take. Tune GUNICORN_WORKERS/_THREADS for
# your concurrent-job load.
exec gunicorn seedview.wsgi:application \
    --bind 0.0.0.0:8000 \
    --worker-class gthread \
    --workers "${GUNICORN_WORKERS:-3}" \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --access-logfile - \
    --error-logfile -
