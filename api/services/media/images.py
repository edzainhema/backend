"""Image processing pipeline: filter application + text-overlay rendering."""
from io import BytesIO

from django.core.files.base import ContentFile
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

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

    # ── Uniform contain-scale + centred offsets ──────────────────────
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


