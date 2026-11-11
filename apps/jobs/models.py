import uuid
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

_TERMINAL_STATUSES = frozenset(["completed", "failed", "cancelled"])


class Job(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Configure"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    class Source(models.TextChoices):
        LOCAL = "local", "Local Upload"
        GOOGLE_DRIVE = "google_drive", "Google Drive"
        SAMPLE = "sample", "Sample Images"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="jobs",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    source = models.CharField(max_length=20, choices=Source.choices)
    # Minimum detection confidence kept when results come back from the model.
    conf = models.FloatField(
        default=0.5,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Set automatically when the job first reaches a terminal state.
    completed_at = models.DateTimeField(null=True, blank=True)
    # Set by the cleanup command once image files have been deleted.
    images_purged = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.user})"

    def save(self, *args, **kwargs):
        # Auto-stamp completed_at the first time status becomes terminal.
        if self.status in _TERMINAL_STATUSES and self.completed_at is None:
            self.completed_at = timezone.now()
            # When update_fields is used, add completed_at so it's persisted.
            if "update_fields" in kwargs:
                kwargs["update_fields"] = list(kwargs["update_fields"]) + ["completed_at"]
        super().save(*args, **kwargs)

    @property
    def image_count(self):
        return self.images.count()

    @property
    def completed_count(self):
        return self.images.filter(status=JobImage.Status.COMPLETED).count()

    def class_totals(self):
        """Per-class detection totals across this job's completed images."""
        from .runpod_client import CLASS_NAMES

        totals = {name: 0 for name in CLASS_NAMES}
        for result in self.images.filter(status=JobImage.Status.COMPLETED).values_list("result", flat=True):
            for name, n in ((result or {}).get("counts") or {}).items():
                totals[name] = totals.get(name, 0) + n
        return totals

    @property
    def is_stoppable(self):
        return self.status in {self.Status.PENDING, self.Status.QUEUED, self.Status.PROCESSING}

    @property
    def status_color(self):
        return {
            self.Status.PENDING: "yellow",
            self.Status.QUEUED: "blue",
            self.Status.PROCESSING: "indigo",
            self.Status.COMPLETED: "green",
            self.Status.FAILED: "red",
            self.Status.CANCELLED: "gray",
        }.get(self.status, "gray")


def _job_image_path(instance, filename):
    return f"tmp/{instance.job_id}/{filename}"


def _job_thumb_path(instance, filename):
    return f"tmp/{instance.job_id}/thumbs/{filename}"


def _job_raw_thumb_path(instance, filename):
    return f"tmp/{instance.job_id}/raw_thumbs/{filename}"


class JobImage(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to=_job_image_path, blank=True)
    # Plain (no overlay) resized copy of `image`, generated at upload time so the
    # configure page can show a lightweight grid instead of full-res originals.
    thumbnail = models.ImageField(upload_to=_job_raw_thumb_path, blank=True)
    overlay_thumbnail = models.ImageField(upload_to=_job_thumb_path, blank=True)
    original_filename = models.CharField(max_length=255)
    drive_file_id = models.CharField(max_length=255, blank=True)
    drive_thumbnail_url = models.URLField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # See runpod_client.run_detection for the shape of a successful result;
    # failures store {"error": "..."}.
    result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return self.original_filename

    @property
    def counts(self):
        return (self.result or {}).get("counts") or {}

    @property
    def error(self):
        return (self.result or {}).get("error", "")
