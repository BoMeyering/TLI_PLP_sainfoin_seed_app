FROM python:3.12.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=seedview.settings.prod

WORKDIR /app

# curl: needed to fetch supercronic below and for the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# supercronic — a container-friendly cron replacement. Unlike vixie-cron it
# doesn't need syslog or root, forwards each job's stdout/stderr to its own
# (so `docker logs` sees cron output), and reads the crontab as an ordinary
# file instead of installing it into /etc/cron.d.
ENV SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.49/supercronic-linux-amd64 \
    SUPERCRONIC_SHA1SUM=e63c11a9726b775a6a11801e81af4f3fb926aa68

RUN curl -fsSLO "$SUPERCRONIC_URL" \
    && echo "${SUPERCRONIC_SHA1SUM}  supercronic-linux-amd64" | sha1sum -c - \
    && chmod +x supercronic-linux-amd64 \
    && mv supercronic-linux-amd64 /usr/local/bin/supercronic

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x docker/entrypoint.sh

# collectstatic only needs settings to import cleanly, not a real secret or a
# live database — this placeholder is never used at runtime, where the real
# SECRET_KEY from the environment takes over.
RUN SECRET_KEY=build-time-placeholder python manage.py collectstatic --noinput

# Same placeholder trick: pre-renders the sample-image thumbnails so the
# "Try a Sample Job" button doesn't re-thumbnail every full-size sample per click.
RUN SECRET_KEY=build-time-placeholder python manage.py build_sample_thumbnails

RUN useradd --create-home --uid 1000 django \
    # media/ is excluded by .dockerignore (real uploads shouldn't be baked
    # into the image), so it never exists here via COPY. It still needs to
    # exist as an empty, django-owned dir: compose mounts a named volume over
    # it at runtime, and Docker only initializes a brand-new volume's
    # ownership by copying up whatever the image had at that path — if
    # nothing's there, it defaults to root-owned, which then makes every
    # write from the (non-root) django user fail silently.
    && mkdir -p /app/media \
    && chown -R django:django /app
    
USER django

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/ -o /dev/null || exit 1

ENTRYPOINT ["docker/entrypoint.sh"]
