from django.contrib import admin
from django.urls import path, re_path, include
from django.conf import settings
from django.views.static import serve as serve_media

from apps.accounts.views import landing

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("", include("apps.jobs.urls")),
    path("", landing, name="landing"),
]

# Uploaded images/thumbnails live outside STATIC_URL (whitenoise only serves
# collectstatic's output), so they need their own route in every environment
# — not just DEBUG, which is what django.conf.urls.static.static() gates on
# internally regardless of the "if settings.DEBUG" this used to be wrapped in.
urlpatterns += [
    re_path(
        r"^media/(?P<path>.*)$",
        serve_media,
        {"document_root": settings.MEDIA_ROOT},
    ),
]
