"""Client for the YOLO26 sainfoin seed/pod detection endpoint on RunPod Serverless.

Endpoint contract (TLI_PLP_sainfoin_detection_runpod_docker_build/runpod_handler.py):

    POST {RUNPOD_BASE_URL}/runsync
    {"input": {"image": "<base64 PNG>"}}

    -> {"id": "...", "status": "COMPLETED", "output": {
           "detection": {"bboxes": "<base64 float32>", "dtype": "float32",
                         "shape": [1000, 6], "errors": null},
           "server_inference_time": 0.21,
           "errors": null}}

Each of the 1000 rows is an end-to-end (NMS-free) YOLO26 prediction:
[x1, y1, x2, y2, confidence, class_id] in the model's input pixel space
(RUNPOD_INFERENCE_SIZE x RUNPOD_INFERENCE_SIZE). The endpoint applies no
confidence threshold, so filtering happens here.

This is a queue-based serverless endpoint, not a load-balanced one: /runsync
can return before the job finishes (status IN_QUEUE / IN_PROGRESS, e.g. while
a worker cold-starts), in which case the job is polled via /status/{id}.
"""

import base64
import io
import logging
import time

import numpy as np
import requests
from django.conf import settings
from PIL import Image, ImageOps

# Ordered to match class_id (list position = class_id), mirroring the `names`
# metadata baked into sainfoin_seed_yolo26_043300.onnx ({0: 'pod', 1: 'seed'}).
# Tied to the model's fixed output classes, so hardcoded here rather than
# fetched at runtime.
CLASS_NAMES = ["pod", "seed"]

logger = logging.getLogger(__name__)

TERMINAL_OK = "COMPLETED"
TERMINAL_FAIL = frozenset(["FAILED", "CANCELLED", "TIMED_OUT"])
POLL_INTERVAL = 1.5
HTTP_TIMEOUT = 60


class RunPodError(Exception):
    pass


def _headers():
    return {
        "Authorization": f"Bearer {settings.RUNPOD_API_KEY}",
        "Content-Type": "application/json",
    }


def _url(path):
    return f"{settings.RUNPOD_BASE_URL}/{path.lstrip('/')}"


def load_oriented_rgb(image_bytes_or_path):
    """Opens an image with EXIF orientation applied, as RGB.

    Every consumer (the payload sent to the model, overlays, thumbnails) goes
    through this so detection coordinates and rendered pixels always agree on
    which way is up.
    """
    src = image_bytes_or_path
    if isinstance(src, (bytes, bytearray)):
        src = io.BytesIO(src)
    image = Image.open(src)
    image = ImageOps.exif_transpose(image)
    return image.convert("RGB")


def prepare_image(image_bytes):
    """Resize to the model's input size and encode as the PNG the handler expects.

    Returns (png_bytes, (orig_width, orig_height)) — the original size is what
    detections get scaled back to.

    The image is stretched to a square (no letterboxing), same as the handler's
    own cv2.resize and the repo's reference client, so boxes scale back with an
    independent x/y factor.
    """
    img = load_oriented_rgb(image_bytes)
    orig_size = img.size
    size = settings.RUNPOD_INFERENCE_SIZE
    img = img.resize((size, size), Image.Resampling.BILINEAR)

    # The handler decodes with cv2.imdecode(..., cv2.IMREAD_COLOR), which always
    # yields a BGR-ordered array, and feeds that straight to the model — but
    # Ultralytics models are trained on RGB. (The repo's reference client gets
    # away with it by reading RGB and then cv2.imencode-ing it as if it were
    # BGR.) Swapping R/B before encoding here means OpenCV's BGR decode lands
    # back on true RGB order. Without it the model finds essentially nothing.
    r, g, b = img.split()
    img = Image.merge("RGB", (b, g, r))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), orig_size


def decode_detections(output, orig_size, conf=0.0):
    """Turns the handler's base64 bbox array into a list of detection dicts in
    original-image pixel coordinates, keeping only those with score >= conf.
    Sorted by confidence, highest first."""
    detection = (output or {}).get("detection") or {}
    if detection.get("errors"):
        raise RunPodError(str(detection["errors"]))
    b64 = detection.get("bboxes")
    if not b64:
        raise RunPodError("Endpoint response is missing detection.bboxes")

    arr = np.frombuffer(base64.b64decode(b64), dtype=detection.get("dtype") or "float32")
    arr = arr.reshape(-1, 6) if arr.size else np.zeros((0, 6), dtype=np.float32)

    size = settings.RUNPOD_INFERENCE_SIZE
    orig_w, orig_h = orig_size
    sx, sy = orig_w / size, orig_h / size

    arr = arr[arr[:, 4] >= conf]
    arr = arr[np.argsort(-arr[:, 4])]

    detections = []
    for x1, y1, x2, y2, score, cls in arr.tolist():
        cls = int(round(cls))
        detections.append({
            "class_id": cls,
            "class_name": CLASS_NAMES[cls] if 0 <= cls < len(CLASS_NAMES) else str(cls),
            "confidence": round(float(score), 4),
            "bbox": [
                round(max(0.0, min(x1 * sx, orig_w)), 1),
                round(max(0.0, min(y1 * sy, orig_h)), 1),
                round(max(0.0, min(x2 * sx, orig_w)), 1),
                round(max(0.0, min(y2 * sy, orig_h)), 1),
            ],
        })
    return detections


def summarize(detections):
    """Per-class counts and mean confidence for a list of detections."""
    counts = {name: 0 for name in CLASS_NAMES}
    conf_sums = {name: 0.0 for name in CLASS_NAMES}
    for d in detections:
        counts[d["class_name"]] = counts.get(d["class_name"], 0) + 1
        conf_sums[d["class_name"]] = conf_sums.get(d["class_name"], 0.0) + d["confidence"]
    mean_conf = {
        name: round(conf_sums[name] / counts[name], 4) if counts[name] else None
        for name in counts
    }
    return {"counts": counts, "total": sum(counts.values()), "mean_confidence": mean_conf}


def _error_detail(response):
    """RunPod's API errors carry the useful part in the body, e.g.
    {"status": 409, "detail": "Endpoint is paused (max_workers=0)...", "code": "ENDPOINT_PAUSED"}."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:300]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error") or body.get("message") or body)[:300]
    return str(body)[:300]


def _request(method, path, timeout, **kwargs):
    try:
        response = requests.request(method, _url(path), headers=_headers(), timeout=timeout, **kwargs)
    except requests.RequestException as e:
        raise RunPodError(f"Could not reach the inference endpoint: {e}") from e
    if response.status_code >= 400:
        if response.status_code in (401, 403):
            raise RunPodError(f"RunPod rejected the API key (HTTP {response.status_code}). Check RUNPOD_API_KEY.")
        raise RunPodError(f"RunPod HTTP {response.status_code}: {_error_detail(response)}")
    try:
        return response.json()
    except ValueError as e:
        raise RunPodError(f"Endpoint returned non-JSON response: {e}") from e


def _post(path, payload, timeout=HTTP_TIMEOUT):
    return _request("POST", path, timeout, json=payload)


def _get(path, timeout=HTTP_TIMEOUT):
    return _request("GET", path, timeout)


def cancel_job(job_id):
    try:
        requests.post(_url(f"cancel/{job_id}"), headers=_headers(), timeout=10)
    except requests.RequestException:
        pass


def _await_job(data, timeout, should_cancel=None):
    """Polls /status/{id} until the RunPod job reaches a terminal state.
    Returns the job's `output`; raises RunPodError on failure/timeout."""
    deadline = time.monotonic() + timeout
    while True:
        status = data.get("status")
        job_id = data.get("id")
        if status == TERMINAL_OK:
            return data.get("output")
        if status in TERMINAL_FAIL:
            raise RunPodError(f"RunPod job {job_id} {status.lower()}: {data.get('error') or data.get('output') or ''}".strip())
        if not job_id:
            raise RunPodError(f"Unexpected endpoint response: {str(data)[:300]}")
        if should_cancel and should_cancel():
            cancel_job(job_id)
            raise RunPodError("Cancelled")
        if time.monotonic() >= deadline:
            cancel_job(job_id)
            raise RunPodError(f"RunPod job {job_id} did not finish within {timeout}s (last status: {status})")
        time.sleep(POLL_INTERVAL)
        data = _get(f"status/{job_id}")


def run_detection(image_bytes, conf=0.5, timeout=None, should_cancel=None):
    """Sends one image through the detector and returns the result dict stored
    on JobImage.result:

        {
          "detections": [{"class_id", "class_name", "confidence", "bbox": [x1, y1, x2, y2]}, ...],
          "counts": {"pod": int, "seed": int}, "total": int,
          "mean_confidence": {"pod": float|None, "seed": float|None},
          "image_size": [w, h], "inference_size": int, "conf": float,
          "server_inference_time": float, "runpod_job_id": str,
        }
    """
    timeout = timeout or settings.RUNPOD_JOB_TIMEOUT
    png_bytes, orig_size = prepare_image(image_bytes)
    payload = {"input": {"image": base64.b64encode(png_bytes).decode("ascii")}}

    data = _post("runsync", payload, timeout=max(HTTP_TIMEOUT, 120))
    output = _await_job(data, timeout, should_cancel=should_cancel)

    if not isinstance(output, dict):
        raise RunPodError(f"Unexpected endpoint output: {str(output)[:300]}")
    if output.get("errors"):
        raise RunPodError(str(output["errors"]))

    detections = decode_detections(output, orig_size, conf=conf)
    return {
        "detections": detections,
        **summarize(detections),
        "image_size": list(orig_size),
        "inference_size": settings.RUNPOD_INFERENCE_SIZE,
        "conf": conf,
        "server_inference_time": output.get("server_inference_time"),
        "runpod_job_id": data.get("id", ""),
    }


def endpoint_health():
    """Returns the endpoint's /health payload, or None if it can't be reached."""
    try:
        return _get("health", timeout=10)
    except RunPodError:
        return None


def _workers_up(health):
    workers = (health or {}).get("workers") or {}
    return sum(int(workers.get(k) or 0) for k in ("idle", "ready", "running")) > 0


def _tiny_png():
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (255, 255, 255)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def check_ready(timeout=None):
    """Makes sure a worker is up and serving, cold-starting one if needed.
    Returns (ready, detail) — detail explains a failure in user-facing terms.

    Serverless endpoints scale to zero and only start a worker once a job is
    queued, so there's nothing to "ping" while cold. If /health shows no live
    worker, this submits a tiny throwaway image and waits for it to complete —
    by then a worker is warm and the real batch won't each pay the cold start.
    """
    timeout = timeout or settings.RUNPOD_WARMUP_TIMEOUT
    try:
        health = _get("health", timeout=10)
    except RunPodError as e:
        logger.warning("RunPod health check failed: %s", e)
        return False, str(e)
    if _workers_up(health):
        return True, ""

    try:
        data = _post("run", {"input": {"image": _tiny_png()}}, timeout=30)
        output = _await_job(data, timeout)
    except RunPodError as e:
        logger.warning("RunPod warm-up failed: %s", e)
        return False, str(e)
    if not isinstance(output, dict) or output.get("errors"):
        detail = f"Warm-up request failed on the worker: {(output or {}).get('errors') if isinstance(output, dict) else output}"
        logger.warning("RunPod warm-up failed: %s", detail)
        return False, detail
    return True, ""


def wait_for_ready(timeout=None):
    """Boolean form of check_ready()."""
    return check_ready(timeout)[0]
