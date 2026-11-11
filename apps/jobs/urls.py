from django.urls import path
from . import views

app_name = "jobs"

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("jobs/submit/", views.job_submit, name="submit"),
    path("jobs/submit/sample/", views.job_submit_sample, name="submit_sample"),
    path("jobs/<uuid:pk>/configure/", views.job_configure, name="configure"),
    path("jobs/<uuid:pk>/warmup/", views.job_warmup, name="warmup"),
    path("jobs/history/", views.job_history, name="history"),
    path("jobs/<uuid:pk>/", views.job_detail, name="detail"),
    path("jobs/<uuid:pk>/counts.csv", views.job_detections_csv, name="detections_csv"),
    path("jobs/<uuid:pk>/boxes.csv", views.job_boxes_csv, name="boxes_csv"),
    path("jobs/<uuid:pk>/images.zip", views.job_images_zip, name="images_zip"),
    path("jobs/images/<int:pk>/overlay.jpg", views.job_image_overlay, name="image_overlay"),
    path("jobs/images/<int:pk>/overlay-thumb.jpg", views.job_image_overlay_thumb, name="image_overlay_thumb"),
    path("jobs/images/<int:pk>/preview.jpg", views.job_image_preview, name="image_preview"),
    path("jobs/<uuid:pk>/stop/", views.stop_job, name="stop"),
    path("jobs/<uuid:pk>/delete/", views.delete_job, name="delete"),
    path("api/google-token/", views.google_token_api, name="google_token_api"),
    path("api/picker-error/", views.picker_error_api, name="picker_error_api"),
]
