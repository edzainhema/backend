"""Image processing pipeline: filter application + text-overlay rendering."""
from io import BytesIO

from django.core.files.base import ContentFile
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from .fonts import resolve_overlay_font_path
from .overlays import _safe_float, _safe_int, _draw_text_overlay


def process_media_image(input_file, metadata):
    """
    Bakes filter + text overlays into an image using Pillow.
    metadata is already a parsed dict (not a string).

    Raises on failure — the caller (create_post) wraps this in an atomic
    transaction so the half-created Post is rolled back instead of being
    silently saved without the user's edits.
    """
    overlays = metadata.get('overlays', [])
    # Coerce every numeric metadata field at the boundary — see the
    # _safe_float / _safe_int docstring above for why.
    filter_index = _safe_int(metadata.get('filter_index'), default=0, lo=0, hi=999)
    preview_w = _safe_float(metadata.get('preview_width'), default=1.0, lo=1.0, hi=100000.0)
    preview_h = _safe_float(metadata.get('preview_height'), default=1.0, lo=1.0, hi=100000.0)

    # Decode straight to RGBA so the semi-transparent text-pill composites
    # correctly. We flatten back to RGB at the end before JPEG encoding.
    img = Image.open(input_file).convert('RGBA')
    img_w, img_h = img.size

    # Apply colour filter via ImageEnhance (approximate)
    # For full accuracy you'd use a colour matrix — this covers the
    # most common cases (contrast, saturation, brightness)
    FILTER_ENHANCE = {
        0: {},
        1: {'contrast': 1.4, 'saturation': 1.4},
        2: {'contrast': 0.85, 'saturation': 0.0, 'brightness': 1.1},
        3: {'saturation': 0.0},
        4: {'contrast': 1.1, 'brightness': 1.05},
        5: {'contrast': 0.9, 'brightness': 1.02},
        6: {'contrast': 1.05},
        7: {'contrast': 0.9, 'brightness': 1.05},
        8: {'contrast': 1.2, 'brightness': 1.03},
    }

    enhancements = FILTER_ENHANCE.get(filter_index, {})
    if 'contrast' in enhancements:
        img = ImageEnhance.Contrast(img).enhance(enhancements['contrast'])
    if 'saturation' in enhancements:
        img = ImageEnhance.Color(img).enhance(enhancements['saturation'])
    if 'brightness' in enhancements:
        img = ImageEnhance.Brightness(img).enhance(enhancements['brightness'])

    # Uniform contain-scale + centred offsets.
    # The mobile preview displays the image with object-fit: contain inside
    # the preview box, so overlay coordinates need the same transform when
    # mapped onto the underlying image pixels. Independent x/y scaling would
    # warp the text positions whenever the preview's aspect ratio doesn't
    # match the image's.
    cover_scale = min(img_w / preview_w, img_h / preview_h)
    offset_x = (img_w - preview_w * cover_scale) / 2
    offset_y = (img_h - preview_h * cover_scale) / 2

    # Draw overlays onto a transparent layer, then composite. This makes the
    # background-pill alpha actually work, and keeps drop shadows clean.
    overlay_layer = Image.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay_layer)
    for ov in overlays:
        if not isinstance(ov, dict):
            # Hostile / malformed payload — skip rather than crash.
            continue
        text = ov.get('text', '')
        if not text:
            continue

        # Coerce client-supplied geometry before doing any arithmetic with it.
        # PIL will happily multiply strings ('10' * float) into a TypeError
        # mid-render, which would roll back the whole post — and the FFmpeg
        # video path needs hardened input regardless (see _safe_float
        # comment above).
        ov_x = _safe_float(ov.get('x'), default=0.0)
        ov_y = _safe_float(ov.get('y'), default=0.0)
        final_x = ov_x * cover_scale + offset_x
        final_y = ov_y * cover_scale + offset_y

        base_font_size = _safe_float(ov.get('fontSize'), default=24.0, lo=1.0, hi=1000.0)
        scaled_font_size = max(1, int(base_font_size * cover_scale))

        # Resolve the user-selected face for this overlay; falls back to a
        # system font on miss. Loading via ImageFont.truetype with the
        # PostScript-named TTF gives us the exact weight the user picked
        # (Caveat-SemiBold, Oswald-Bold, etc.).
        font_path = resolve_overlay_font_path(
            ov.get('fontFamily'),
            ov.get('fontWeight'),
        )
        try:
            if font_path:
                font = ImageFont.truetype(font_path, scaled_font_size)
            else:
                font = ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()

        has_background = ov.get('hasBackground', True)
        _draw_text_overlay(
            draw, text, font, final_x, final_y, cover_scale,
            has_background,
        )

    # Composite overlays onto the (filtered) image, then flatten for JPEG.
    img = Image.alpha_composite(img, overlay_layer).convert('RGB')

    buffer = BytesIO()
    img.save(buffer, format='JPEG', quality=92)
    buffer.seek(0)
    return ContentFile(buffer.getvalue(), name=input_file.name)


# Long edge (px) of the grid/feed thumbnail baked for image posts. The media
# grid renders 3 columns of ~130px-wide cells on the largest phones, so a
# 400px-long-edge JPEG is comfortably above 2x for those tiles while decoding
# to a sub-1MB bitmap (vs the ~48MB a full-res 12MP photo decodes to). Keeping
# the decoded footprint tiny is what stops the bitmap cache from evicting grid
# tiles on scroll-back and flashing the grey placeholder.
IMAGE_THUMBNAIL_MAX_EDGE = 400
IMAGE_THUMBNAIL_QUALITY = 80


def make_image_thumbnail(
    input_file,
    max_edge=IMAGE_THUMBNAIL_MAX_EDGE,
    quality=IMAGE_THUMBNAIL_QUALITY,
):
    """Downscale an image to a small JPEG thumbnail for the media grid.

    Returns a ``ContentFile`` (JPEG bytes) on success, or ``None`` on any
    failure — exactly like the video first-frame path, thumbnail generation is
    best-effort and must never break an otherwise-valid upload (the grid falls
    back to the full-res ``file`` when ``thumbnail`` is null).

    The read cursor of ``input_file`` is reset to 0 both before reading and in a
    ``finally`` on the way out, so the SAME object can still be handed straight
    to ``PostMedia.file`` for storage afterwards — mirroring the seek discipline
    in ``_image_dimensions`` (create.py). Never upscales: ``Image.thumbnail``
    only shrinks, so small source images are stored as-is.
    """
    try:
        input_file.seek(0)
        with Image.open(input_file) as img:
            # Bake EXIF orientation into the pixels. Phone cameras often store
            # the raw sensor pixels plus an "Orientation" tag telling the viewer
            # to rotate 90/180/270°. The client honours that tag on the original
            # file, but a re-encoded thumbnail drops it — so without this the
            # grid thumbnail renders sideways/upside-down while the full image
            # looks correct. exif_transpose rotates the pixels and removes the
            # now-redundant tag; it is a no-op for images with no orientation.
            img = ImageOps.exif_transpose(img)
            # Flatten to RGB (JPEG has no alpha); compositing onto white keeps
            # transparent PNGs from going black. ``thumbnail`` preserves aspect
            # ratio and only ever reduces, so portrait/landscape both work and
            # an already-small image is left untouched.
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGBA')
                background = Image.new('RGBA', img.size, (255, 255, 255, 255))
                img = Image.alpha_composite(background, img).convert('RGB')
            else:
                img = img.convert('RGB')

            img.thumbnail((max_edge, max_edge), Image.LANCZOS)

            buffer = BytesIO()
            img.save(buffer, format='JPEG', quality=quality, optimize=True)
            buffer.seek(0)
            return ContentFile(buffer.getvalue())
    except Exception:
        return None
    finally:
        try:
            input_file.seek(0)
        except Exception:
            pass


# Maximum portrait aspect ratio for stored media: 9:16 (width:height). Media
# TALLER than this — i.e. height > width * 16/9 — is center-cropped down to
# exactly 9:16 at upload; square/landscape/shorter-portrait media passes through
# untouched. Matches the render-time cap in the feed row (MAX_MEDIA_HEIGHT =
# width * 16/9) and the HLS crop baked in the worker, so a clip is the same shape
# in storage, in the stream, and on screen.
MAX_PORTRAIT_RATIO = 16 / 9


def crop_to_max_portrait_ratio(input_file, max_ratio=MAX_PORTRAIT_RATIO):
    """Center-crop an image's top+bottom so it is no taller than ``max_ratio``
    (height / width). Returns a JPEG ``ContentFile`` of the cropped image, or
    ``None`` when no crop was needed (already within the cap) OR on any failure
    — in both cases the caller keeps the original bytes unchanged.

    EXIF orientation is baked into the pixels first (``exif_transpose``) so the
    crop is taken against the image as it actually displays — otherwise a
    portrait photo stored as landscape-pixels + a rotate tag would be cropped on
    the wrong axis. Seek-safe: the read cursor is reset on the way out so the
    SAME object can still be measured / stored afterwards (mirrors
    ``make_image_thumbnail`` / ``average_color``).
    """
    try:
        input_file.seek(0)
        with Image.open(input_file) as img:
            img = ImageOps.exif_transpose(img)
            w, h = img.size
            if w <= 0 or h <= 0:
                return None
            max_h = int(round(w * max_ratio))
            # Keep the crop height even — libx264/yuv420p (and our video path)
            # want even dims, and it costs nothing for stills to match.
            max_h -= max_h % 2
            if max_h <= 0 or h <= max_h:
                return None  # already within the 9:16 cap → no crop
            top = (h - max_h) // 2
            img = img.convert("RGB").crop((0, top, w, top + max_h))
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=92)
            buffer.seek(0)
            return ContentFile(
                buffer.getvalue(),
                name=getattr(input_file, "name", None) or "crop.jpg",
            )
    except Exception:
        return None
    finally:
        try:
            input_file.seek(0)
        except Exception:
            pass


def average_color(input_file):
    """Return the image's average colour as a hex string ("#rrggbb"), or None.

    Painted as the tile background so a photo fades in over a matched colour
    instead of a hard grey box while loading. Computed by collapsing the image
    to a single pixel (PIL averages during the resize) — fast and plenty for a
    placeholder. Best-effort and seek-safe like make_image_thumbnail, so the
    SAME file object can still be stored afterwards.
    """
    try:
        input_file.seek(0)
        with Image.open(input_file) as img:
            img = ImageOps.exif_transpose(img).convert('RGB').resize((1, 1))
            r, g, b = img.getpixel((0, 0))
            return f'#{r:02x}{g:02x}{b:02x}'
    except Exception:
        return None
    finally:
        try:
            input_file.seek(0)
        except Exception:
            pass
