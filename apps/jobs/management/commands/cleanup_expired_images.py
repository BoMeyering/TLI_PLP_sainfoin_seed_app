"""
Management command: cleanup_expired_images

Deletes the upload directory for every job that reached a terminal state
(completed / failed / cancelled) more than IMAGE_RETENTION_HOURS hours ago.

Each job's images are stored under:
    MEDIA_ROOT/tmp/<job-uuid>/

The entire directory is removed with shutil.rmtree so no files or empty
subdirectories are left behind. The Job and JobImage database records are
preserved so historical counts and metadata remain intact.

Recommended cron (runs every hour):
    0 * * * * /path/to/.venv/bin/python /path/to/manage.py cleanup_expired_images

With logging:
    0 * * * * /path/to/.venv/bin/python /path/to/manage.py cleanup_expired_images \
              >> /path/to/logs/cleanup.log 2>&1
"""

import shutil
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import models as db_models
from django.utils import timezone

from apps.jobs.models import Job


class Command(BaseCommand):
    help = "Delete tmp upload directories for jobs whose terminal state is older than N hours."

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=24,
            help="Retention window in hours (default: 24).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without touching any files.",
        )

    def handle(self, *args, **options):
        retention_hours = options["hours"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(hours=retention_hours)
        media_root = Path(settings.MEDIA_ROOT)

        terminal = [Job.Status.COMPLETED, Job.Status.FAILED, Job.Status.CANCELLED]

        jobs = Job.objects.filter(
            status__in=terminal,
            images_purged=False,
        ).filter(
            # Use completed_at when available; fall back to updated_at for
            # legacy rows that predate the completed_at field.
            db_models.Q(completed_at__lte=cutoff)
            | db_models.Q(completed_at__isnull=True, updated_at__lte=cutoff)
        )

        total_jobs = 0
        total_freed = 0

        for job in jobs:
            job_dir = media_root / "tmp" / str(job.pk)

            dir_size = _dir_size_mb(job_dir) if job_dir.exists() else 0

            if dry_run:
                status = "exists" if job_dir.exists() else "already gone"
                self.stdout.write(
                    f"  [dry-run] '{job.name}' — {job_dir} ({status}, ~{dir_size:.1f} MB)"
                )
            else:
                if job_dir.exists():
                    shutil.rmtree(job_dir)

                # Clear the image field on every related JobImage so the DB
                # no longer references a path that no longer exists.
                job.images.update(image="")

                job.images_purged = True
                job.save(update_fields=["images_purged"])

                self.stdout.write(
                    f"  Purged '{job.name}' ({job.pk}) — ~{dir_size:.1f} MB freed."
                )

            total_jobs += 1
            total_freed += dir_size

        prefix = "[dry-run] " if dry_run else ""
        style = self.style.WARNING if dry_run else self.style.SUCCESS
        self.stdout.write(
            style(
                f"\n{prefix}Done: {total_jobs} job(s) processed, "
                f"~{total_freed:.1f} MB {('would be ' if dry_run else '')}freed."
            )
        )


def _dir_size_mb(path: Path) -> float:
    """Return the total size of a directory tree in megabytes."""
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1_048_576
    except OSError:
        return 0.0
