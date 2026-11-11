import logging

from allauth.account.signals import user_logged_in
from django.dispatch import receiver

logger = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


@receiver(user_logged_in)
def log_google_granted_scopes(request, user, sociallogin=None, **kwargs):
    """Logs the scopes Google actually granted on each Google sign-in.

    Google's consent screen lets users untick individual scopes, so a login
    can succeed without Drive access — the Picker then 403s in the browser,
    which never shows up in our own request logs.
    """
    if sociallogin is None or sociallogin.account.provider != "google":
        return
    # Google echoes the granted scopes on the OAuth callback query string.
    granted = request.GET.get("scope", "").split()
    if DRIVE_SCOPE in granted:
        logger.info("Google login for user %s (%s) granted Drive scope", user.pk, user.email)
    else:
        logger.warning(
            "Google login for user %s (%s) did NOT grant Drive scope; Drive Picker will fail. Granted: %s",
            user.pk,
            user.email,
            " ".join(granted) or "(none reported)",
        )
