import base64
import csv
import io
import shutil
import tempfile
from unittest import mock

import numpy as np
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.accounts.models import User

from . import runpod_client
from .models import Job, JobImage
from .overlay import build_overlay_png, render_detections
from .runpod_client import RunPodError, decode_detections, prepare_image, run_detection, wait_for_ready

SIZE = 512


def _jpeg_bytes(w=1024, h=768, color=(200, 120, 40)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return buf.getvalue()


def _raw_output(rows):
    """Builds a handler `output` dict from [x1, y1, x2, y2, conf, cls] rows,
    padded to the model's fixed 1000 rows like the real endpoint."""
    arr = np.zeros((1000, 6), dtype=np.float32)
    if rows:
        arr[: len(rows)] = np.array(rows, dtype=np.float32)
    return {
        "detection": {
            "bboxes": base64.b64encode(arr.tobytes()).decode(),
            "dtype": "float32",
            "shape": [1000, 6],
            "errors": None,
        },
        "server_inference_time": 0.1,
        "errors": None,
    }


ROWS = [
    [100, 100, 120, 120, 0.95, 1],  # seed
    [200, 200, 230, 220, 0.80, 0],  # pod
    [300, 300, 310, 310, 0.30, 1],  # seed below default threshold
]


def _resp(payload, status=200):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload
    return r


def _route(req):
    """The client goes through requests.request(); route that to req.post /
    req.get so tests can script and assert on each verb separately."""
    req.request.side_effect = lambda method, url, **kw: (req.post if method == "POST" else req.get)(url, **kw)
    return req


@override_settings(RUNPOD_INFERENCE_SIZE=SIZE, RUNPOD_BASE_URL="https://rp.test/v2/abc", RUNPOD_API_KEY="k")
class RunPodClientTests(TestCase):
    def test_prepare_image_resizes_and_swaps_red_blue(self):
        png, orig = prepare_image(_jpeg_bytes(1024, 768, (255, 0, 0)))
        self.assertEqual(orig, (1024, 768))
        img = Image.open(io.BytesIO(png))
        self.assertEqual(img.format, "PNG")
        self.assertEqual(img.size, (SIZE, SIZE))
        # Pure red goes out stored as blue, so cv2's BGR decode reads it back as RGB red.
        r, g, b = img.convert("RGB").getpixel((10, 10))
        self.assertLess(r, 20)
        self.assertGreater(b, 230)

    def test_prepare_image_applies_exif_orientation(self):
        buf = io.BytesIO()
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 CW on display
        Image.new("RGB", (400, 200)).save(buf, format="JPEG", exif=exif)
        _, orig = prepare_image(buf.getvalue())
        self.assertEqual(orig, (200, 400))

    def test_decode_detections_filters_scales_and_sorts(self):
        dets = decode_detections(_raw_output(ROWS), (1024, 2048), conf=0.5)
        self.assertEqual([d["class_name"] for d in dets], ["seed", "pod"])
        self.assertEqual(dets[0]["bbox"], [200.0, 400.0, 240.0, 480.0])  # x*2, y*4
        self.assertAlmostEqual(dets[1]["confidence"], 0.8, places=4)

    def test_decode_detections_raises_on_handler_error(self):
        with self.assertRaises(RunPodError):
            decode_detections({"detection": {"errors": "boom"}}, (10, 10))

    @mock.patch("apps.jobs.runpod_client.time.sleep")
    @mock.patch("apps.jobs.runpod_client.requests")
    def test_run_detection_polls_until_complete(self, req, _sleep):
        req.RequestException = Exception
        _route(req)
        req.post.return_value = _resp({"id": "j1", "status": "IN_QUEUE"})
        req.get.side_effect = [
            _resp({"id": "j1", "status": "IN_PROGRESS"}),
            _resp({"id": "j1", "status": "COMPLETED", "output": _raw_output(ROWS)}),
        ]
        result = run_detection(_jpeg_bytes(1024, 1024), conf=0.5)

        url = req.post.call_args.args[0]
        self.assertEqual(url, "https://rp.test/v2/abc/runsync")
        self.assertIn("image", req.post.call_args.kwargs["json"]["input"])
        self.assertEqual(req.post.call_args.kwargs["headers"]["Authorization"], "Bearer k")
        self.assertEqual(req.get.call_args.args[0], "https://rp.test/v2/abc/status/j1")

        self.assertEqual(result["counts"], {"pod": 1, "seed": 1})
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["image_size"], [1024, 1024])
        self.assertEqual(result["runpod_job_id"], "j1")

    @mock.patch("apps.jobs.runpod_client.requests")
    def test_run_detection_raises_on_failed_job(self, req):
        req.RequestException = Exception
        _route(req)
        req.post.return_value = _resp({"id": "j1", "status": "FAILED", "error": "CUDA OOM"})
        with self.assertRaisesRegex(RunPodError, "CUDA OOM"):
            run_detection(_jpeg_bytes())

    @mock.patch("apps.jobs.runpod_client.requests")
    def test_run_detection_raises_on_handler_errors(self, req):
        req.RequestException = Exception
        _route(req)
        req.post.return_value = _resp({"id": "j1", "status": "COMPLETED", "output": {"errors": "bad image"}})
        with self.assertRaisesRegex(RunPodError, "bad image"):
            run_detection(_jpeg_bytes())

    @mock.patch("apps.jobs.runpod_client.requests")
    def test_wait_for_ready_true_when_worker_idle(self, req):
        req.RequestException = Exception
        _route(req)
        req.get.return_value = _resp({"workers": {"idle": 1, "ready": 0, "running": 0}})
        self.assertTrue(wait_for_ready(timeout=5))
        req.post.assert_not_called()

    @mock.patch("apps.jobs.runpod_client.time.sleep")
    @mock.patch("apps.jobs.runpod_client.requests")
    def test_wait_for_ready_cold_starts_with_warmup_job(self, req, _sleep):
        req.RequestException = Exception
        _route(req)
        req.get.side_effect = [
            _resp({"workers": {"idle": 0, "ready": 0, "running": 0}}),
            _resp({"id": "w1", "status": "COMPLETED", "output": _raw_output([])}),
        ]
        req.post.return_value = _resp({"id": "w1", "status": "IN_QUEUE"})
        self.assertTrue(wait_for_ready(timeout=5))
        self.assertEqual(req.post.call_args.args[0], "https://rp.test/v2/abc/run")

    @mock.patch("apps.jobs.runpod_client.requests")
    def test_paused_endpoint_reports_runpod_detail(self, req):
        req.RequestException = Exception
        _route(req)
        req.get.return_value = _resp({"workers": {"idle": 0, "ready": 0, "running": 0}})
        req.post.return_value = _resp(
            {"status": 409, "detail": "Endpoint is paused (max_workers=0).", "code": "ENDPOINT_PAUSED"}, status=409)
        ready, detail = runpod_client.check_ready(timeout=5)
        self.assertFalse(ready)
        self.assertIn("max_workers=0", detail)
        with self.assertRaisesRegex(RunPodError, "HTTP 409: Endpoint is paused"):
            run_detection(_jpeg_bytes())

    @mock.patch("apps.jobs.runpod_client.requests")
    def test_wait_for_ready_false_when_unreachable(self, req):
        req.RequestException = ConnectionError
        _route(req)
        req.get.side_effect = ConnectionError("down")
        self.assertFalse(wait_for_ready(timeout=5))


class OverlayTests(TestCase):
    def test_render_draws_class_colored_boxes_at_scaled_positions(self):
        result = {
            "image_size": [1000, 1000],
            "detections": [{"class_name": "seed", "confidence": 0.9, "bbox": [100, 100, 300, 300]}],
            "counts": {"seed": 1, "pod": 0},
        }
        out = render_detections(Image.new("RGB", (500, 500), (255, 255, 255)), result, badge=False)
        self.assertEqual(out.size, (500, 500))
        r, g, b = out.getpixel((50, 100))  # left edge of the (scaled) box
        self.assertLess(r, 50)  # seed color is cyan
        self.assertEqual(out.getpixel((400, 400)), (255, 255, 255))


@override_settings(
    RUNPOD_INFERENCE_SIZE=SIZE, RUNPOD_BASE_URL="https://rp.test/v2/abc",
    RUNPOD_API_KEY="k", RUNPOD_MAX_CONCURRENCY=2,
)
class JobFlowTests(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        self.user = User.objects.create_user(username="u", email="u@example.com", password="pw")
        self.client.force_login(self.user)
        self.job = Job.objects.create(user=self.user, name="Lot A", source=Job.Source.LOCAL, status=Job.Status.QUEUED)
        self.images = []
        for i in range(3):
            img = JobImage.objects.create(job=self.job, original_filename=f"s{i}.jpg")
            img.image.save(f"s{i}.jpg", ContentFile(_jpeg_bytes(1024, 1024)), save=True)
            self.images.append(img)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)

    def _fake_detection(self, image_bytes, conf=0.5, timeout=None, should_cancel=None):
        png, orig = prepare_image(image_bytes)
        dets = decode_detections(_raw_output(ROWS), orig, conf=conf)
        return {"detections": dets, **runpod_client.summarize(dets), "image_size": list(orig),
                "inference_size": SIZE, "conf": conf, "server_inference_time": 0.1, "runpod_job_id": "x"}

    def _run_job(self, conf="0.5"):
        with mock.patch("apps.jobs.views.check_ready", return_value=(True, "")), \
             mock.patch("apps.jobs.views.run_detection", side_effect=self._fake_detection):
            resp = self.client.post(reverse("jobs:configure", args=[self.job.pk]), {"conf": conf})
            self.assertEqual(resp["Content-Type"], "text/event-stream")
            return b"".join(resp.streaming_content).decode()

    def test_configure_streams_progress_and_completes(self):
        body = self._run_job()
        self.assertIn("event: progress", body)
        self.assertIn('"done": 3, "total": 3', body)
        self.assertIn("event: done", body)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.COMPLETED)
        self.assertEqual(self.job.class_totals(), {"pod": 3, "seed": 3})
        for img in self.job.images.all():
            self.assertEqual(img.status, JobImage.Status.COMPLETED)
            self.assertTrue(img.overlay_thumbnail)

    def test_lower_threshold_keeps_more_detections(self):
        self._run_job(conf="0.25")
        self.job.refresh_from_db()
        self.assertEqual(self.job.class_totals(), {"pod": 3, "seed": 6})

    def test_failed_images_are_recorded_and_job_fails_when_all_fail(self):
        with mock.patch("apps.jobs.views.check_ready", return_value=(True, "")), \
             mock.patch("apps.jobs.views.run_detection", side_effect=RunPodError("endpoint exploded")):
            resp = self.client.post(reverse("jobs:configure", args=[self.job.pk]), {"conf": "0.5"})
            b"".join(resp.streaming_content)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.FAILED)
        self.assertEqual(self.job.images.filter(status=JobImage.Status.FAILED).count(), 3)
        self.assertIn("endpoint exploded", self.job.images.first().error)

    def test_endpoint_not_ready_fails_job(self):
        with mock.patch("apps.jobs.views.check_ready", return_value=(False, "Endpoint is paused (max_workers=0).")):
            resp = self.client.post(reverse("jobs:configure", args=[self.job.pk]), {"conf": "0.5"})
        self.assertRedirects(resp, reverse("jobs:detail", args=[self.job.pk]))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.FAILED)

    def test_detail_page_and_exports(self):
        self._run_job()
        detail = self.client.get(reverse("jobs:detail", args=[self.job.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "seeds detected")
        self.assertContains(detail, "overlay-images-data")

        counts = self.client.get(reverse("jobs:detections_csv", args=[self.job.pk]))
        rows = list(csv.DictReader(io.StringIO(counts.content.decode())))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["seed_count"], "1")
        self.assertEqual(rows[0]["pod_count"], "1")
        self.assertEqual(rows[0]["total_count"], "2")

        first = self.images[0].pk
        boxes = self.client.get(reverse("jobs:boxes_csv", args=[self.job.pk]) + f"?ids={first}")
        rows = list(csv.DictReader(io.StringIO(boxes.content.decode())))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["class_name"], "seed")
        self.assertEqual(rows[0]["x1"], "200.0")  # 100 * (1024 / 512)

        zip_resp = self.client.get(reverse("jobs:images_zip", args=[self.job.pk]))
        self.assertEqual(zip_resp["Content-Type"], "application/zip")

        for name in ("image_overlay", "image_preview", "image_overlay_thumb"):
            r = self.client.get(reverse(f"jobs:{name}", args=[first]))
            self.assertEqual(r.status_code, 200, name)
            self.assertEqual(r["Content-Type"], "image/jpeg")

    def test_overlay_png_renders_from_saved_result(self):
        self._run_job()
        img = self.job.images.first()
        data = build_overlay_png(img, max_dim=200, fmt="JPEG")
        self.assertEqual(Image.open(io.BytesIO(data)).size, (200, 200))

    def test_other_users_cannot_see_job(self):
        other = User.objects.create_user(username="o", email="o@example.com", password="pw")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("jobs:detail", args=[self.job.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("jobs:image_preview", args=[self.images[0].pk])).status_code, 404)
