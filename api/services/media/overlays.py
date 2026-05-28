"""Text-overlay drawing for image/video pipelines + the shared numeric
coercion helpers it uses. Imported by both .images and .video."""
import math



def _safe_float(value, default, lo=-100000.0, hi=100000.0):
    """Coerce `value` to a finite float within [lo, hi]; else return default."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    if f < lo or f > hi:
        return default
    return f


def _safe_int(value, default, lo=-100000, hi=100000):
    """Coerce `value` to an int within [lo, hi]; else return default."""
    if value is None:
        return default
    try:
        # Going via float first so '12.0' / 12.0 both work, matches the
        # mobile app which may serialise some ints as JSON numbers with a
        # decimal point.
        i = int(float(value))
    except (TypeError, ValueError):
        return default
    if i < lo or i > hi:
        return default
    return i


def _safe_optional_float(value, lo=-100000.0, hi=100000.0):
    """Like _safe_float, but returns None on missing/bad input instead of a
    default. Used for the measured-baseline fields where we want to fall
    through to the legacy positioning branch when the client didn't send a
    valid measurement."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    if f < lo or f > hi:
        return None
    return f


# Background-pill padding + corner radius, in *preview DIP*. These mirror the
# editor (DraggableText.tsx: paddingHorizontal 20 / paddingVertical 10 /
# borderRadius 12) and the client image baker (exportImage.ts uses the exact
# same constants). They're ABSOLUTE DIP values — they do NOT scale with the
# font size — so the bake multiplies them by the preview→output `scale` to land
# the same gap between the glyphs and the pill edge that the user saw while
# editing. (The previous value here, scaled_font_size * 0.3, scaled with the
# font and came out far tighter than the editor at the sizes it actually
# offers.)
_OVERLAY_PAD_X_DIP = 20.0   # DraggableText styles.text.paddingHorizontal
_OVERLAY_PAD_Y_DIP = 10.0   # DraggableText styles.text.paddingVertical
_OVERLAY_RADIUS_DIP = 12.0  # DraggableText styles.text.borderRadius


def _draw_text_overlay(draw, text, font, final_x, final_y, scale,
                       has_background):
    """
    Draw a single text overlay — a rounded background pill (or a drop shadow)
    plus the white glyphs — onto an RGBA ``ImageDraw`` surface.

    Shared by the image baker (process_media_image) and the video baker
    (process_media_video) so a baked video's overlays come out pixel-identical
    to a baked image's. The pill wraps the text's *tight* bounding box
    (``draw.textbbox``) with the editor's padding on each axis, so the glyphs
    sit dead-centre in the pill AND the gap to the pill edge matches what the
    user saw in the editor. Drawing the text with Pillow here (rather than
    FFmpeg's ``drawtext``) is what keeps it centred: drawtext and Pillow
    disagree on where ``y`` places the text, so a Pillow pill wrapped around
    drawtext glyphs left the text riding visibly high.

    ``final_x`` / ``final_y`` are the Pillow text origin in output-pixel space;
    ``scale`` is the preview-DIP → output-pixel factor (cover_scale for images,
    preview_scale for videos) used to size the pill padding and corner radius.
    """
    if has_background:
        # Background pill on the RGBA layer — alpha is preserved. Pad the tight
        # glyph box by the editor's DIP padding × scale (horizontal vs vertical
        # are different, exactly like the editor) so the gap matches the
        # preview; the symmetric pad on each axis keeps the glyphs centred.
        bbox = draw.textbbox((final_x, final_y), text, font=font)
        pad_x = _OVERLAY_PAD_X_DIP * scale
        pad_y = _OVERLAY_PAD_Y_DIP * scale
        radius = _OVERLAY_RADIUS_DIP * scale
        rect = [
            bbox[0] - pad_x,
            bbox[1] - pad_y,
            bbox[2] + pad_x,
            bbox[3] + pad_y,
        ]
        # Clamp the radius so it can't exceed half the pill's smaller side
        # (Pillow draws a malformed shape otherwise on very short text).
        r = max(1.0, min(radius, (rect[2] - rect[0]) / 2, (rect[3] - rect[1]) / 2))
        draw.rounded_rectangle(rect, radius=r, fill=(0, 0, 0, 128))
    else:
        # Drop shadow for legibility on bright backgrounds.
        draw.text(
            (final_x + 2, final_y + 2),
            text,
            fill=(0, 0, 0, 200),
            font=font,
        )

    draw.text((final_x, final_y), text, fill=(255, 255, 255, 255), font=font)


