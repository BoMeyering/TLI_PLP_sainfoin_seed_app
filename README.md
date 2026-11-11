# TLI_PLP_sainfoin_seed_app

**SeedView**: a Django app that detects and counts sainfoin seeds and pods in photos of seed samples. It sends each image to the YOLO26 detector deployed on RunPod Serverless ([TLI_PLP_sainfoin_detection_runpod_docker_build](https://github.com/Oxbow-Solutions/TLI_PLP_sainfoin_detection_runpod_docker_build)).

The app is a port of `pgcview_v2_app`. It keeps the same auth (Google OAuth via allauth), the same upload sources (local files, Google Drive Picker, built-in samples), the same job lifecycle (upload, configure, process with live SSE progress, review, export), and the same Docker/cron deployment. The inference pipeline and the result rendering were rewritten for object detection.

## Pipeline

```
upload → Configure (confidence threshold) → warm-up → per-image detection → review & export
```

1. **Warm-up** (`runpod_client.wait_for_ready`). Checks `GET /health`. If no worker is idle, ready, or running, it submits a tiny throwaway image via `POST /run` and waits for it to finish. Serverless endpoints scale to zero, so this absorbs the cold start once instead of once per image.
2. **Prepare** (`runpod_client.prepare_image`). Applies EXIF orientation, resizes to `RUNPOD_INFERENCE_SIZE`² (512 by default), swaps the R and B channels, and encodes the result as PNG. The swap is needed because the handler decodes with OpenCV, which yields BGR, and passes that array straight to an RGB-trained model. Without the swap the model detects almost nothing.
3. **Infer.** Sends `POST /runsync {"input": {"image": <b64 PNG>}}`. If the job is still `IN_QUEUE` or `IN_PROGRESS` when `/runsync` returns, the client polls `GET /status/{id}`. Up to `RUNPOD_MAX_CONCURRENCY` images are in flight at once.
4. **Decode** (`runpod_client.decode_detections`). The handler returns the raw end-to-end YOLO26 output: a base64 float32 array of shape `(1000, 6)` with columns `[x1, y1, x2, y2, conf, class_id]`, in 512-px input coordinates, with no threshold applied. The app keeps detections with `conf ≥ job.conf`, scales the boxes back to original-image pixels, and stores them on `JobImage.result`:

   ```json
   {"detections": [{"class_id": 1, "class_name": "seed", "confidence": 0.957, "bbox": [x1, y1, x2, y2]}],
    "counts": {"pod": 1, "seed": 81}, "total": 82, "mean_confidence": {"pod": 0.81, "seed": 0.9},
    "image_size": [4608, 3456], "inference_size": 512, "conf": 0.5,
    "server_inference_time": 0.21, "runpod_job_id": "..."}
   ```

Classes come from the ONNX metadata: `0 = pod`, `1 = seed` (`runpod_client.CLASS_NAMES`).

## Results and exports

- **Job page:** per-class totals, plus a thumbnail grid with the boxes drawn on and per-image seed and pod counts.
- **Lightbox:** a crisp SVG box overlay with confidence shown on hover, per-class show/hide toggles, and a download of the full-resolution overlay.
- **Exports** (all of them or a selected subset):
  - `counts.csv`: per-image counts and mean confidence.
  - `boxes.csv`: one row per detection, with class, confidence, `x1,y1,x2,y2`, size and centre, in original pixel coordinates.
  - `images.zip`: full-resolution JPEG overlays with a count legend.

## Configuration

Copy `.env.example` to `.env` for local development. For Docker, copy `.env.docker.example` to `.env.docker`. The RunPod-specific settings are:

| Variable | Default | |
|---|---|---|
| `RUNPOD_BASE_URL` | `https://api.runpod.ai/v2/39hov69mmwkuf9` | Serverless endpoint base URL |
| `RUNPOD_API_KEY` | (none) | RunPod API key |
| `RUNPOD_INFERENCE_SIZE` | `512` | Must match the handler's `decode_img()` expected size (see note below) |
| `RUNPOD_MAX_CONCURRENCY` | `4` | Images in flight at once |
| `RUNPOD_JOB_TIMEOUT` | `300` | Seconds per image, including queue wait and cold start |
| `RUNPOD_WARMUP_TIMEOUT` | `180` | Seconds the Configure page waits for a worker |

> **Note on input size:** the model was exported at `imgsz=1024`. However, the handler's `decode_img(expected_size=786432)` equals 512×512×3, so every input is resized to 512×512 server-side, whatever you send. In local tests, 1024 input found slightly more objects, mostly pods. If the handler is changed to accept 1024×1024, set `RUNPOD_INFERENCE_SIZE=1024` to match.

## Local development

```bash
uv venv -p 3.12 .venv && uv pip install -r requirements.txt
cp .env.example .env   # fill in SECRET_KEY, Google OAuth, RUNPOD_API_KEY
python manage.py migrate
python manage.py build_sample_thumbnails   # optional; speeds up "Try a Sample Job"
python manage.py runserver
python manage.py test apps                 # unit tests (RunPod calls are mocked)
```

## Deployment

The deployment steps are the same as pgcview's:

```bash
./build_container.sh            # or run the "Release" GitHub Action to publish to GHCR
cp .env.docker.example .env.docker && $EDITOR .env.docker
./compose_up.sh
```

Inside the container, `supercronic` runs `cleanup_expired_images` every hour (it removes image files 24 h after a job finishes and keeps the results) and `purge_old_jobs` every night (it deletes jobs older than 7 days).
