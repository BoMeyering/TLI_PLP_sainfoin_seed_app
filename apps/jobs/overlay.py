import io

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .runpod_client import CLASS_NAMES

# Keyed by class name, in CLASS_NAMES order. Single source of truth for the
# rendered overlays, the lightbox's SVG boxes, and the count chips/legend.
# Rose vs. cyan: both read clearly against white sample trays and brown seed
# coats, and stay distinguishable for the common forms of color blindness.
CLASS_INFO = [
    ("pod", (225, 29, 72)),
    ("seed", (6, 182, 212)),
]
assert [name for name, _ in CLASS_INFO] == CLASS_NAMES, "CLASS_INFO must follow the model's class order"

CLASS_COLORS = dict(CLASS_INFO)
CLASS_COLORS_HEX = {name: "#%02x%02x%02x" % rgb for name, rgb in CLASS_INFO}

THUMBNAIL_MAX_DIM = 320

# Box fill opacity (0-255) — light enough that the seed itself stays visible.
FILL_ALPHA = 48
# Below this rendered size the count badge would be unreadable, so skip it.
BADGE_MIN_DIM = 800


def open_image(path, max_dim=None):
    """Opens `path` EXIF-oriented as RGB, downscaled to fit max_dim if given.

    For JPEGs, draft() lets libjpeg decode straight at a reduced scale, which
    is several times faster than a full 16 MP decode followed by a resize —
    the difference that matters when rendering a grid of thumbnails.
    """
    image = Image.open(path)
    if max_dim:
        image.draft("RGB", (max_dim, max_dim))
    image = ImageOps.exif_transpose(image).convert("RGB")
    if max_dim and max(image.size) > max_dim:
        image.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
    return image


def _encode(image, fmt, quality):
    buf = io.BytesIO()
    if fmt == "JPEG":
        image.save(buf, format="JPEG", quality=quality)
    else:
        image.save(buf, format="PNG")
    return buf.getvalue()


def build_thumbnail_jpeg(path, max_dim):
    """Plain (no overlay) resized copy of an uploaded image, for lightweight grid
    display before inference has run. Returns JPEG bytes."""
    return _encode(open_image(path, max_dim), "JPEG", 82)


def build_preview_jpeg(path, max_dim, quality=85):
    """Plain resized copy for the lightbox, which draws its own SVG boxes on top."""
    return _encode(open_image(path, max_dim), "JPEG", quality)


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no sized default font
        return ImageFont.load_default()


def _draw_count_badge(image, counts):
    """Top-left legend with per-class counts, so exported overlays are
    self-describing without the app around them."""
    w, h = image.size
    font_size = max(14, round(min(w, h) / 40))
    font = _font(font_size)
    pad = round(font_size * 0.6)
    swatch = round(font_size * 0.8)
    gap = round(font_size * 0.5)

    lines = [(name, f"{name}: {counts.get(name, 0)}") for name in CLASS_NAMES]
    measure = ImageDraw.Draw(image)
    text_w = max(measure.textlength(text, font=font) for _, text in lines)
    line_h = round(font_size * 1.35)
    box_w = round(pad * 2 + swatch + gap + text_w)
    box_h = pad * 2 + line_h * len(lines)

    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    margin = pad
    draw.rounded_rectangle(
        [margin, margin, margin + box_w, margin + box_h],
        radius=pad, fill=(0, 0, 0, 165),
    )
    for i, (name, text) in enumerate(lines):
        top = margin + pad + i * line_h
        sw_top = top + (line_h - swatch) / 2
        draw.rectangle(
            [margin + pad, sw_top, margin + pad + swatch, sw_top + swatch],
            fill=CLASS_COLORS.get(name, (255, 255, 255)) + (255,),
        )
        draw.text(
            (margin + pad + swatch + gap, top + line_h / 2),
            text, font=font, fill=(255, 255, 255, 255), anchor="lm",
        )
    return Image.alpha_composite(image, layer)


def render_detections(image, result, badge=None):
    """Draws `result["detections"]` (original-image pixel coords) onto `image`,
    which may be a downscaled copy of the original. Returns a new RGB image."""
    orig_w, orig_h = result.get("image_size") or image.size
    w, h = image.size
    sx, sy = w / orig_w, h / orig_h
    line = max(1, round(min(w, h) / 450))

    canvas = image.convert("RGBA")
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # Lowest confidence first, so the strongest boxes end up drawn on top.
    for det in sorted(result.get("detections") or [], key=lambda d: d["confidence"]):
        x1, y1, x2, y2 = det["bbox"]
        rect = [x1 * sx, y1 * sy, x2 * sx, y2 * sy]
        rgb = CLASS_COLORS.get(det["class_name"], (255, 255, 0))
        draw.rectangle(rect, fill=rgb + (FILL_ALPHA,))
        draw.rectangle(rect, outline=rgb + (255,), width=line)

    canvas = Image.alpha_composite(canvas, layer)

    if badge is None:
        badge = min(w, h) >= BADGE_MIN_DIM or max(w, h) >= BADGE_MIN_DIM * 1.5
    if badge:
        canvas = _draw_count_badge(canvas, result.get("counts") or {})
    return canvas.convert("RGB")


def build_overlay_png(job_image, max_dim=None, fmt="PNG", quality=90):
    """Render the detected seed/pod boxes over the original image.
    Returns encoded image bytes in `fmt` ("PNG" or "JPEG"; `quality` only applies to JPEG).

    If max_dim is given, the original is downscaled to fit within it first, so all
    drawing runs on the smaller image (cheap thumbnails vs. full-res).
    """
    image = open_image(job_image.image.path, max_dim)
    return _encode(render_detections(image, job_image.result or {}), fmt, quality)
