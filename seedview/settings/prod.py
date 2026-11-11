from .base import *  # noqa: F401,F403

DEBUG = False

# "localhost" is always allowed in addition to the real domain(s) from env:
# the container's own HEALTHCHECK curls http://localhost:8000/ from inside
# the container, which never leaves the Docker network, so it carries none
# of the external-Host-header-spoofing risk ALLOWED_HOSTS otherwise guards
# against.
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[]) + ["localhost"]
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://seedview:seedview@db:5432/seedview"),
}

# Cookies only ever travel over HTTPS in production (TLS terminated upstream —
# see SECURE_PROXY_SSL_HEADER in base.py).
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# No transactional email in this app (Google-only auth, ACCOUNT_EMAIL_VERIFICATION
# is "none") — console backend avoids a slow/failing SMTP attempt if anything
# ever does call send_mail.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
