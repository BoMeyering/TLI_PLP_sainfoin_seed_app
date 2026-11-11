import csv
import json
import logging
import shutil
import threading
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from io import BytesIO
from pathlib import Path
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST

from .forms import JobConfigureForm, JobSubmitForm
from .models import Job, JobImage
from .overlay import CLASS_COLORS_HEX, THUMBNAIL_MAX_DIM, build_overlay_png, build_preview_jpeg, build_thumbnail_jpeg
from .runpod_client import CLASS_NAMES, RunPodError, check_ready, run_detection
from .utils import download_drive_image, get_google_access_token, populate_sample_images, token_has_drive_scope

logger = logging.getLogger(__name__)

MODAL_MAX_DIM = 1600
OVERLAY_CACHE_CONTROL = "private, max-age=86400, immutable"
# Seconds between SSE keepalive comments while waiting on the endpoint — a
# cold start can leave the stream silent for minutes otherwise, long enough
# for a reverse proxy or browser to give up on it.
KEEPALIVE_INTERVAL = 10


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _parse_ids_param(request):
    """Returns a list of ints from a comma-separated ?ids= param, or None if absent/invalid."""
    raw = request.GET.get("ids")
    if not raw:
        return None
    try:
        return [int(x) for x in raw.split(",") if x]
    except ValueError:
        return None


@login_required
def dashboard(request):
    jobs = Job.objects.filter(user=request.user)
    stats = jobs.aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(status__in=["pending", "queued"])),
        processing=Count("id", filter=Q(status="processing")),
        completed=Count("id", filter=Q(status="completed")),
        failed=Count("id", filter=Q(status="failed")),
    )
    recent_jobs = jobs[:10]
    return render(request, "jobs/dashboard.html", {
        "stats": stats,
        "recent_jobs": recent_jobs,
    })


@login_required
def job_history(request):
    jobs = Job.objects.filter(user=request.user)
    status_filter = request.GET.get("status", "")
    if status_filter:
        jobs = jobs.filter(status=status_filter)
    return render(request, "jobs/history.html", {
        "jobs": jobs,
        "status_filter": status_filter,
        "status_choices": Job.Status.choices,
    })


@login_required
def job_detail(request, pk):
    job = get_object_or_404(Job, pk=pk, user=request.user)
    completed = list(job.images.filter(status=JobImage.Status.COMPLETED))
    overlay_images = [
        {
            "id": img.pk,
            "name": img.original_filename,
            "url": reverse("jobs:image_preview", args=[img.pk]),
            "download_url": reverse("jobs:image_overlay", args=[img.pk]),
            "size": (img.result or {}).get("image_size") or [1, 1],
            "counts": img.counts,
            # Compact keys — this is inlined into the page for every box.
            "boxes": [
                {"c": d["class_name"], "s": d["confidence"], "b": d["bbox"]}
                for d in (img.result or {}).get("detections") or []
            ],
        }
        for img in completed
    ] if not job.images_purged else []
    all_image_ids = list(job.images.values_list("pk", flat=True))

    totals = {name: 0 for name in CLASS_NAMES}
    for img in completed:
        for name, n in img.counts.items():
            totals[name] = totals.get(name, 0) + n
    class_totals = [
        {"name": name, "count": totals.get(name, 0), "color": CLASS_COLORS_HEX.get(name, "#999999")}
        for name in CLASS_NAMES
    ]
    return render(request, "jobs/detail.html", {
        "job": job,
        "overlay_images": overlay_images,
        "all_image_ids": all_image_ids,
        "class_colors": CLASS_COLORS_HEX,
        "class_totals": class_totals,
        "completed_count": len(completed),
        "failed_count": job.images.filter(status=JobImage.Status.FAILED).count(),
    })


def _selected_images(request, job):
    images = job.images.all()
    ids = _parse_ids_param(request)
    if ids is not None:
        images = images.filter(pk__in=ids)
    return images


def _csv_response(job, suffix):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{slugify(job.name)}-{suffix}.csv"'
    return response


@login_required
def job_detections_csv(request, pk):
    """One row per image: per-class counts and mean confidence."""
    job = get_object_or_404(Job, pk=pk, user=request.user)
    response = _csv_response(job, "counts")

    writer = csv.writer(response)
    writer.writerow(
        ["image_name", "status"]
        + [f"{name}_count" for name in CLASS_NAMES]
        + ["total_count"]
        + [f"{name}_mean_confidence" for name in CLASS_NAMES]
        + ["conf_threshold", "image_width", "image_height", "error"]
    )

    for img in _selected_images(request, job):
        result = img.result or {}
        counts = result.get("counts") or {}
        mean_conf = result.get("mean_confidence") or {}
        width, height = (result.get("image_size") or ["", ""])[:2]
        done = img.status == JobImage.Status.COMPLETED
        writer.writerow(
            [img.original_filename, img.status]
            + [counts.get(name, 0) if done else "" for name in CLASS_NAMES]
            + [result.get("total", "") if done else ""]
            + [mean_conf.get(name) if mean_conf.get(name) is not None else "" for name in CLASS_NAMES]
            + [result.get("conf", job.conf if done else ""), width, height, result.get("error", "")]
        )

    return response


@login_required
def job_boxes_csv(request, pk):
    """One row per detection, in original-image pixel coordinates."""
    job = get_object_or_404(Job, pk=pk, user=request.user)
    response = _csv_response(job, "boxes")

    writer = csv.writer(response)
    writer.writerow([
        "image_name", "detection_id", "class_name", "confidence",
        "x1", "y1", "x2", "y2", "width", "height", "center_x", "center_y",
    ])
    images = _selected_images(request, job).filter(status=JobImage.Status.COMPLETED)
    for img in images:
        for i, det in enumerate((img.result or {}).get("detections") or [], start=1):
            x1, y1, x2, y2 = det["bbox"]
            writer.writerow([
                img.original_filename, i, det["class_name"], det["confidence"],
                x1, y1, x2, y2,
                round(x2 - x1, 1), round(y2 - y1, 1),
                round((x1 + x2) / 2, 1), round((y1 + y2) / 2, 1),
            ])

    return response


@login_required
def job_submit(request):
    form = JobSubmitForm()
    google_token = get_google_access_token(request.user)
    if request.method == "POST":
        form = JobSubmitForm(request.POST)
        source = request.POST.get("source", "local")
        if form.is_valid():
            job = form.save(commit=False)
            job.user = request.user
            job.source = source
            job.save()

            if source == "local":
                files = request.FILES.getlist("images")
                if not files:
                    job.delete()
                    form.add_error(None, "Please select at least one image file.")
                    return render(request, "jobs/submit.html", {
                        "form": form,
                        "google_token": google_token,
                        "google_api_key": settings.GOOGLE_API_KEY,
                    })
                for f in files:
                    img = JobImage.objects.create(
                        job=job,
                        image=f,
                        original_filename=f.name,
                    )
                    thumb_bytes = build_thumbnail_jpeg(img.image.path, max_dim=THUMBNAIL_MAX_DIM)
                    img.thumbnail.save(f"{img.pk}.jpg", ContentFile(thumb_bytes), save=True)

                job.status = Job.Status.QUEUED
                job.save(update_fields=["status", "updated_at"])

                messages.success(request, f'{job.image_count} image(s) uploaded. Configure the job to start processing.')
                return redirect("jobs:configure", pk=job.pk)

            elif source == "google_drive":
                raw = request.POST.get("drive_files", "[]")
                drive_files = json.loads(raw)
                if not drive_files:
                    job.delete()
                    form.add_error(None, "Please select at least one file from Google Drive.")
                    return render(request, "jobs/submit.html", {
                        "form": form,
                        "google_token": google_token,
                        "google_api_key": settings.GOOGLE_API_KEY,
                    })

                job.status = Job.Status.QUEUED
                job.save(update_fields=["status", "updated_at"])

                # Set here (not inside the generator below) so SessionMiddleware saves it —
                # once streaming starts, the response has already left process_response.
                messages.success(request, f'{len(drive_files)} image(s) uploaded. Configure the job to start processing.')

                def event_stream():
                    # The XHR upload-progress event the local-file path relies on only
                    # measures request-body bytes — for Drive jobs the body is just a
                    # small JSON list of file ids, so it hits 100% almost instantly
                    # while the server is still fetching files from Drive behind the
                    # scenes. Stream real per-file progress instead, same pattern as
                    # the processing progress on the configure page.
                    total = len(drive_files)
                    for i, file_info in enumerate(drive_files, start=1):
                        name = file_info.get("name", "unknown")
                        yield f"event: progress\ndata: {json.dumps({'current': i, 'total': total, 'filename': name})}\n\n"

                        img = JobImage.objects.create(
                            job=job,
                            original_filename=name,
                            drive_file_id=file_info.get("id", ""),
                            drive_thumbnail_url=file_info.get("thumbnailLink", ""),
                        )
                        download_drive_image(img, request.user)

                    yield f"event: done\ndata: {json.dumps({'redirect': reverse('jobs:configure', args=[job.pk])})}\n\n"

                response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
                response["Cache-Control"] = "no-cache"
                response["X-Accel-Buffering"] = "no"
                return response

    return render(request, "jobs/submit.html", {
        "form": form,
        "google_token": google_token,
        "google_api_key": settings.GOOGLE_API_KEY,
    })


@login_required
@require_POST
def job_submit_sample(request):
    """Creates a job from the baked-in sample images, skipping the upload form
    entirely, so a new user can see a real result without supplying their own images."""
    job = Job.objects.create(
        user=request.user,
        name="Sample Job",
        description="Auto-generated from the app's built-in sample images.",
        source=Job.Source.SAMPLE,
        status=Job.Status.QUEUED,
    )
    populate_sample_images(job)
    messages.success(request, f'{job.image_count} sample image(s) loaded. Configure the job to start processing.')
    return redirect("jobs:configure", pk=job.pk)


def _detect_image(path, conf, should_cancel):
    """Runs in a worker thread: no ORM access here, only file + HTTP I/O."""
    with open(path, "rb") as f:
        image_bytes = f.read()
    return run_detection(image_bytes, conf=conf, should_cancel=should_cancel)


def _job_cancelled(job):
    return Job.objects.filter(pk=job.pk, status=Job.Status.CANCELLED).exists()


def _process_job_stream(job):
    """SSE generator that sends every image in `job` through the detector,
    saving results as they arrive. Images are sent RUNPOD_MAX_CONCURRENCY at a
    time; all DB writes stay on this (the request's) thread."""
    job.status = Job.Status.PROCESSING
    job.save(update_fields=["status", "updated_at"])

    images = list(job.images.all())
    total = len(images)
    done = 0
    any_succeeded = False
    cancel_event = threading.Event()

    def finish(img, result, status):
        nonlocal done, any_succeeded
        img.result = result
        img.status = status
        if status == JobImage.Status.COMPLETED:
            any_succeeded = True
            try:
                thumb_bytes = build_overlay_png(img, max_dim=THUMBNAIL_MAX_DIM, fmt="JPEG", quality=85)
                img.overlay_thumbnail.save(f"{img.pk}.jpg", ContentFile(thumb_bytes), save=False)
            except Exception:
                # Thumbnail is a nice-to-have; the detail page falls back to on-demand rendering.
                logger.exception("Failed to render overlay thumbnail for JobImage %s", img.pk)
        img.save(update_fields=["status", "result", "overlay_thumbnail"])
        done += 1

    yield _sse("progress", {"done": 0, "total": total, "filename": ""})

    pending = []
    for img in images:
        if img.image and Path(img.image.path).exists():
            pending.append(img)
        else:
            finish(img, {"error": "Image file is missing (the upload or Google Drive download may have failed)."},
                   JobImage.Status.FAILED)
            yield _sse("progress", {"done": done, "total": total, "filename": img.original_filename})
    JobImage.objects.filter(pk__in=[img.pk for img in pending]).update(status=JobImage.Status.PROCESSING)

    pool = ThreadPoolExecutor(max_workers=max(1, settings.RUNPOD_MAX_CONCURRENCY))
    futures = {
        pool.submit(_detect_image, img.image.path, job.conf, cancel_event.is_set): img
        for img in pending
    }
    cancelled = False
    try:
        outstanding = set(futures)
        while outstanding:
            finished, outstanding = wait(outstanding, timeout=KEEPALIVE_INTERVAL, return_when=FIRST_COMPLETED)
            for future in finished:
                img = futures[future]
                if future.cancelled():
                    continue
                try:
                    finish(img, future.result(), JobImage.Status.COMPLETED)
                except RunPodError as e:
                    if cancel_event.is_set():
                        img.status = JobImage.Status.PENDING
                        img.save(update_fields=["status"])
                        continue
                    logger.warning("Detection failed for JobImage %s: %s", img.pk, e)
                    finish(img, {"error": str(e)}, JobImage.Status.FAILED)
                except Exception as e:
                    logger.exception("Unexpected error processing JobImage %s", img.pk)
                    finish(img, {"error": f"Unexpected error: {e}"}, JobImage.Status.FAILED)
                yield _sse("progress", {"done": done, "total": total, "filename": img.original_filename})

            if not finished:
                yield ": keepalive\n\n"

            if _job_cancelled(job):
                # Stopped from the detail page. In-flight requests notice the
                # event at their next status poll and cancel their RunPod job;
                # their images go back to pending in the finally below.
                cancelled = True
                cancel_event.set()
                break
    except GeneratorExit:
        # Client went away mid-stream (tab closed, network drop). Nothing will
        # resume this generator, so stop outstanding work and leave the job in
        # a terminal state instead of "processing" forever.
        cancelled = True
        cancel_event.set()
        Job.objects.filter(pk=job.pk).exclude(status=Job.Status.CANCELLED).update(status=Job.Status.CANCELLED)
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if cancel_event.is_set():
            job.images.filter(status=JobImage.Status.PROCESSING).update(status=JobImage.Status.PENDING)
            job.refresh_from_db(fields=["status", "completed_at"])
            if job.status != Job.Status.CANCELLED:
                job.status = Job.Status.CANCELLED
                job.save(update_fields=["status", "updated_at"])

    if not cancelled:
        job.status = Job.Status.COMPLETED if any_succeeded else Job.Status.FAILED
        job.save(update_fields=["status", "updated_at"])

    yield _sse("done", {"redirect": reverse("jobs:detail", args=[job.pk])})


@login_required
def job_configure(request, pk):
    job = get_object_or_404(Job, pk=pk, user=request.user)
    if job.status != Job.Status.QUEUED:
        return redirect("jobs:detail", pk=job.pk)

    form = JobConfigureForm(instance=job)
    if request.method == "POST":
        form = JobConfigureForm(request.POST, instance=job)
        if form.is_valid():
            job = form.save()

            ready, detail = check_ready()
            if not ready:
                job.status = Job.Status.FAILED
                job.save(update_fields=["status", "updated_at"])
                messages.error(request, f"The inference endpoint is not available: {detail or 'it did not start in time'}. Please try again.")
                return redirect("jobs:detail", pk=job.pk)

            # Set here (not inside the generator) so SessionMiddleware saves it —
            # once streaming starts, the response has already left process_response.
            messages.success(request, f'Job "{job.name}" submitted with {job.image_count} image(s).')

            response = StreamingHttpResponse(_process_job_stream(job), content_type="text/event-stream")
            response["Cache-Control"] = "no-cache"
            response["X-Accel-Buffering"] = "no"
            return response

    return render(request, "jobs/configure.html", {
        "form": form,
        "job": job,
        "class_legend": [{"name": n, "color": CLASS_COLORS_HEX[n]} for n in CLASS_NAMES],
    })


@login_required
def job_image_overlay(request, pk):
    img = get_object_or_404(JobImage, pk=pk, job__user=request.user)
    if img.status != JobImage.Status.COMPLETED or not img.result or not img.image:
        raise Http404
    jpeg_bytes = build_overlay_png(img, fmt="JPEG", quality=92)
    response = HttpResponse(jpeg_bytes, content_type="image/jpeg")
    response["Cache-Control"] = OVERLAY_CACHE_CONTROL
    if request.GET.get("download"):
        stem = Path(img.original_filename).stem or f"image_{img.pk}"
        response["Content-Disposition"] = f'attachment; filename="{stem}_overlay.jpg"'
    return response


@login_required
def job_image_preview(request, pk):
    """Plain downscaled image for the lightbox, which draws the boxes itself as SVG."""
    img = get_object_or_404(JobImage, pk=pk, job__user=request.user)
    if not img.image or not Path(img.image.path).exists():
        raise Http404
    response = HttpResponse(build_preview_jpeg(img.image.path, MODAL_MAX_DIM), content_type="image/jpeg")
    response["Cache-Control"] = OVERLAY_CACHE_CONTROL
    return response


@login_required
def job_images_zip(request, pk):
    """Zips the full-resolution overlay images for the given (or all) completed images."""
    job = get_object_or_404(Job, pk=pk, user=request.user)
    images = job.images.filter(status=JobImage.Status.COMPLETED)
    ids = _parse_ids_param(request)
    if ids is not None:
        images = images.filter(pk__in=ids)

    buf = BytesIO()
    used_names = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for img in images:
            if not img.result or not img.image:
                continue
            try:
                # JPEG, not PNG: these are photos, not line art, so lossy
                # compression is imperceptible here but shrinks a multi-image
                # zip drastically (users don't need pixel-exact overlays for
                # viewing/reporting).
                jpeg_bytes = build_overlay_png(img, fmt="JPEG", quality=90)
            except Exception:
                continue
            stem = Path(img.original_filename).stem or f"image_{img.pk}"
            name, n = f"{stem}_overlay.jpg", 1
            while name in used_names:
                name = f"{stem}_overlay_{n}.jpg"
                n += 1
            used_names.add(name)
            zf.writestr(name, jpeg_bytes)

    if not used_names:
        raise Http404

    response = HttpResponse(buf.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{slugify(job.name)}-overlays.zip"'
    return response


@login_required
def job_image_overlay_thumb(request, pk):
    """Fallback for images completed before overlay_thumbnail existed. New images get their
    thumbnail pre-made in job_configure and are served straight from storage instead of hitting this."""
    img = get_object_or_404(JobImage, pk=pk, job__user=request.user)
    if img.status != JobImage.Status.COMPLETED or not img.result or not img.image:
        raise Http404
    response = HttpResponse(build_overlay_png(img, max_dim=THUMBNAIL_MAX_DIM, fmt="JPEG", quality=85), content_type="image/jpeg")
    response["Cache-Control"] = OVERLAY_CACHE_CONTROL
    return response


@login_required
@require_POST
def job_warmup(request, pk):
    """Waits for a warm endpoint worker (cold-starting one if needed), for the Configure page's status pill."""
    get_object_or_404(Job, pk=pk, user=request.user)
    ready, detail = check_ready()
    return JsonResponse({"ready": ready, "detail": detail})


@login_required
@require_POST
def stop_job(request, pk):
    job = get_object_or_404(Job, pk=pk, user=request.user)
    if job.is_stoppable:
        job.status = Job.Status.CANCELLED
        job.save(update_fields=["status", "updated_at"])
        messages.success(request, f'Job "{job.name}" has been stopped.')
    else:
        messages.warning(
            request,
            f'"{job.name}" is already {job.get_status_display().lower()} and cannot be stopped.',
        )
    return redirect(request.POST.get("next") or "jobs:dashboard")


@login_required
@require_POST
def delete_job(request, pk):
    job = get_object_or_404(Job, pk=pk, user=request.user)
    job_name = job.name
    job_dir = Path(settings.MEDIA_ROOT) / "tmp" / str(job.pk)
    if job_dir.exists():
        shutil.rmtree(job_dir)
    job.delete()
    messages.success(request, f'Job "{job_name}" has been permanently deleted.')
    return redirect("jobs:history")


@login_required
@require_GET
def google_token_api(request):
    """Returns the user's current Google OAuth access token for the Drive Picker."""
    token = get_google_access_token(request.user)
    drive_scope_missing = False
    if not token:
        logger.warning(
            "No Google access token available for user %s (%s); Drive Picker cannot open",
            request.user.pk,
            request.user.email,
        )
    elif token_has_drive_scope(token) is False:
        drive_scope_missing = True
        logger.warning(
            "Google token for user %s (%s) lacks Drive scope; prompting re-sign-in",
            request.user.pk,
            request.user.email,
        )
    return JsonResponse({
        "token": token,
        "api_key": settings.GOOGLE_API_KEY,
        "drive_scope_missing": drive_scope_missing,
    })


@login_required
@require_POST
def picker_error_api(request):
    """Logs a client-side Google Picker failure, which otherwise only ever
    shows up in the user's browser console."""
    try:
        detail = json.loads(request.body or b"{}")
    except ValueError:
        detail = {}
    logger.warning(
        "Drive Picker error for user %s (%s): stage=%s message=%s",
        request.user.pk,
        request.user.email,
        str(detail.get("stage", ""))[:100],
        str(detail.get("message", ""))[:500],
    )
    return HttpResponse(status=204)
