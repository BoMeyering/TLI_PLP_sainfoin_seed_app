import io
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from .overlay import THUMBNAIL_MAX_DIM, build_thumbnail_jpeg

SAMPLE_IMAGES_DIR = settings.BASE_DIR / "sample_images"
# Pre-rendered by `manage.py build_sample_thumbnails` (run in the Dockerfile).
SAMPLE_THUMBS_DIR = SAMPLE_IMAGES_DIR / "thumbs"
SAMPLE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}

# Refresh a bit before the real expiry so a token doesn't die mid-request.
TOKEN_REFRESH_MARGIN = timedelta(seconds=60)

logger = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


def get_google_access_token(user):
    """Returns a valid Google OAuth access token for the user, refreshing the
    stored one first if it's expired (or close to it).

    Access tokens are short-lived (~1hr); without this, a token fetched once
    at login or page-load time goes stale and every Drive API/Picker call
    starts 401/403ing until the user logs out and back in to force a fresh
    OAuth round-trip.
    """
    try:
        token = user.socialaccount_set.get(provider="google").socialtoken_set.first()
    except Exception:
        return None
    if not token:
        return None

    if token.expires_at and token.expires_at > timezone.now() + TOKEN_REFRESH_MARGIN:
        return token.token

    if not token.token_secret:
        # No refresh_token on file (e.g. it was issued before `access_type=offline`
        # was added, or Google only grants one on first consent) — nothing to
        # refresh with, so fall back to whatever's stored.
        return token.token

    app_config = settings.SOCIALACCOUNT_PROVIDERS.get("google", {}).get("APP", {})
    credentials = Credentials(
        token=token.token,
        refresh_token=token.token_secret,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=app_config.get("client_id"),
        client_secret=app_config.get("secret"),
    )
    try:
        credentials.refresh(Request())
    except RefreshError:
        # Refresh token revoked/invalid — the user will need to sign in again.
        return None

    token.token = credentials.token
    if credentials.expiry:
        token.expires_at = timezone.make_aware(credentials.expiry) if timezone.is_naive(credentials.expiry) else credentials.expiry
    token.save(update_fields=["token", "expires_at"])
    return token.token


def token_has_drive_scope(access_token):
    """Returns whether Google says `access_token` carries the Drive scope.

    Users can untick Drive on Google's consent screen and still sign in, which
    leaves the Picker 403ing in the browser. Returns None if Google can't be
    reached, so callers don't block the Picker on a transient failure.
    """
    try:
        resp = requests.get(TOKENINFO_URL, params={"access_token": access_token}, timeout=5)
    except requests.RequestException:
        logger.exception("Google tokeninfo lookup failed")
        return None
    if resp.status_code != 200:
        # Google answers 400 for an invalid/expired token.
        return False
    return DRIVE_SCOPE in resp.json().get("scope", "").split()


def sample_image_paths():
    """The baked-in sample images, sorted by name (camera exports are often .JPG)."""
    return sorted(
        p for p in SAMPLE_IMAGES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in SAMPLE_IMAGE_SUFFIXES
    )


def sample_thumb_path(path):
    return SAMPLE_THUMBS_DIR / f"{path.stem}.jpg"


def populate_sample_images(job):
    """Attaches the baked-in sample images (see `sample_images/` at the repo root)
    to `job` as JobImage rows, same as a local upload would."""
    from .models import JobImage

    for path in sample_image_paths():
        img = JobImage.objects.create(job=job, original_filename=path.name)
        img.image.save(path.name, ContentFile(path.read_bytes()), save=True)
        prebuilt = sample_thumb_path(path)
        if prebuilt.exists():
            thumb_bytes = prebuilt.read_bytes()
        else:
            thumb_bytes = build_thumbnail_jpeg(img.image.path, max_dim=THUMBNAIL_MAX_DIM)
        img.thumbnail.save(f"{img.pk}.jpg", ContentFile(thumb_bytes), save=True)


def download_drive_image(job_image, user):
    """Download a Google Drive file and attach it to the JobImage instance."""
    access_token = get_google_access_token(user)
    if not access_token or not job_image.drive_file_id:
        return False

    try:
        credentials = Credentials(token=access_token)
        service = build("drive", "v3", credentials=credentials)

        request = service.files().get_media(fileId=job_image.drive_file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        buffer.seek(0)
        filename = job_image.original_filename or f"{job_image.drive_file_id}.jpg"
        job_image.image.save(filename, ContentFile(buffer.read()), save=True)

        # Same lightweight preview local uploads get — without it the configure
        # page falls back to the full-res original, which is what was loading
        # so slowly for Drive-sourced jobs.
        thumb_bytes = build_thumbnail_jpeg(job_image.image.path, max_dim=THUMBNAIL_MAX_DIM)
        job_image.thumbnail.save(f"{job_image.pk}.jpg", ContentFile(thumb_bytes), save=True)
        return True
    except Exception:
        logger.exception(
            "Failed to download Drive file %s for JobImage %s",
            job_image.drive_file_id,
            job_image.pk,
        )
        return False
