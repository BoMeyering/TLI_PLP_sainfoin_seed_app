"""
Management command: purge_old_jobs

Permanently deletes jobs older than the retention window: the Job row, its
JobImage rows (via cascade), and its media directory under
MEDIA_ROOT/tmp/<job-uuid>/ (image, thumbnail, and overlay_thumbnail all live
under that one directory, so removing it covers all three).

Unlike cleanup_expired_images (which only clears media shortly after a job
finishes, keeping the historical DB rows around for the job history page),
this is a full delete — nothing about the job survives once it's purged.

Age is measured from Job.created_at regardless of status, so a job that got
stuck pending/processing and never reached a terminal state still gets
cleaned up eventually rather than lingering forever.

Recommended cron (nightly, e.g. 3am):
    0 3 * * * /path/to/.venv/bin/python /path/to/manage.py purge_old_jobs

With logging:
    0 3 * * * /path/to/.venv/bin/python /path/to/manage.py purge_old_jobs \
              >> /path/to/logs/purge.log 2>&1
"""

import shutil
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.jobs.models import Job


class Command(BaseCommand):
    help = "Permanently delete jobs (DB rows + media) older than N days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Retention window in days (default: 7).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without touching anything.",
        )

    def handle(self, *args, **options):
        retention_days = options["days"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(days=retention_days)
        media_root = Path(settings.MEDIA_ROOT)

        jobs = Job.objects.filter(created_at__lte=cutoff)

        total_jobs = 0
        total_images = 0
        total_freed = 0

        for job in jobs:
            job_dir = media_root / "tmp" / str(job.pk)
            dir_size = _dir_size_mb(job_dir) if job_dir.exists() else 0
            image_count = job.image_count

            if dry_run:
                status = "exists" if job_dir.exists() else "already gone"
                self.stdout.write(
                    f"  [dry-run] '{job.name}' ({job.pk}) — {image_count} image(s), "
                    f"{job_dir} ({status}, ~{dir_size:.1f} MB)"
                )
            else:
                if job_dir.exists():
                    shutil.rmtree(job_dir)
                job.delete()  # cascades to JobImage
                self.stdout.write(
                    f"  Deleted '{job.name}' ({job.pk}) — {image_count} image(s), "
                    f"~{dir_size:.1f} MB freed."
                )

            total_jobs += 1
            total_images += image_count
            total_freed += dir_size

        prefix = "[dry-run] " if dry_run else ""
        style = self.style.WARNING if dry_run else self.style.SUCCESS
        self.stdout.write(
            style(
                f"\n{prefix}Done: {total_jobs} job(s) / {total_images} image(s) "
                f"processed, ~{total_freed:.1f} MB {('would be ' if dry_run else '')}freed."
            )
        )


def _dir_size_mb(path: Path) -> float:
    """Return the total size of a directory tree in megabytes."""
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1_048_576
    except OSError:
        return 0.0
