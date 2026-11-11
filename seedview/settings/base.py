from pathlib import Path
import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])

# Behind Caddy's reverse proxy, requests arrive over plain HTTP with X-Forwarded-Proto
# set — without this, Django thinks every request is insecure (breaks CSRF/cookies).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
]

THIRD_PARTY_APPS = [
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
]

LOCAL_APPS = [
    "apps.accounts",
    "apps.jobs",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "seedview.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "seedview.wsgi.application"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"

SITE_ID = 1

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# ── allauth ──────────────────────────────────────────────────────────────────
ACCOUNT_LOGIN_METHODS = {"email"}    # login via email (social OAuth handles this)
ACCOUNT_SIGNUP_FIELDS = ["email*"]  # email required; no password (social-only)
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_ALLOW_REGISTRATION = False  # No manual email/password signup
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_STORE_TOKENS = True
# Auto-connect Google account to existing user with the same email (fixes login loop)
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": env("GOOGLE_OAUTH2_CLIENT_ID", default=""),
            "secret": env("GOOGLE_OAUTH2_CLIENT_SECRET", default=""),
            "key": "",
        },
        "SCOPE": [
            "profile",
            "email",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
        "AUTH_PARAMS": {
            "access_type": "offline",
        },
        "OAUTH_PKCE_ENABLED": True,
    }
}

GOOGLE_API_KEY = env("GOOGLE_API_KEY", default="")

# ── RunPod inference API ────────────────────────────────────────────────────
# Serverless (queue-based) endpoint running the YOLO26 sainfoin seed/pod
# detector — see apps/jobs/runpod_client.py for the request/response contract.
RUNPOD_BASE_URL = env("RUNPOD_BASE_URL", default="https://api.runpod.ai/v2/39hov69mmwkuf9").rstrip("/")
RUNPOD_API_KEY = env("RUNPOD_API_KEY", default="")
# Square size images are resized to before upload. The handler force-resizes
# anything that isn't 512x512x3 to 512x512, so sending exactly that keeps the
# payload small without changing what the model sees. Only change this if the
# handler's decode_img() expected size changes too.
RUNPOD_INFERENCE_SIZE = env.int("RUNPOD_INFERENCE_SIZE", default=512)
# How many images are in flight against the endpoint at once. Serverless
# scales workers out to meet queued jobs, so >1 overlaps cold starts/network
# round-trips; with a single max worker the extra jobs just wait in its queue.
RUNPOD_MAX_CONCURRENCY = env.int("RUNPOD_MAX_CONCURRENCY", default=4)
# Per-image ceiling (seconds) covering queue wait + cold start + inference.
RUNPOD_JOB_TIMEOUT = env.int("RUNPOD_JOB_TIMEOUT", default=300)
# How long the Configure page waits for a worker to come up before giving up.
RUNPOD_WARMUP_TIMEOUT = env.int("RUNPOD_WARMUP_TIMEOUT", default=180)
