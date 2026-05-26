"""
Media upload pipeline: image and video processing, font resolution, and
the safety helpers (size caps, magic-byte sniffing, decompression-bomb
guard) used before we ever touch the bytes a client uploaded.

Extracted from the monolithic views.py during the 2026-05 refactor — see
docs/REFACTOR.md.
"""
import logging
import math
import mimetypes
import os
import re
import subprocess
import tempfile
from io import BytesIO

import ffmpeg
from django.conf import settings
from django.core.files.base import ContentFile
from PIL import (
    Image, ImageDraw, ImageEnhance, ImageFont, UnidentifiedImageError,
)

from ..video_filters import VIDEO_FILTER_CHAINS

logger = logging.getLogger(__name__)


# ── Upload safety limits ────────────────────────────────────────────────────
# These caps protect the server from cheap DoS via oversized uploads and
# decompression bombs. Keep IMAGE_MAX_BYTES generous enough for a high-res
# phone JPEG (~10 MB on iPhone) but tight enough that an attacker can't fill
# the disk with one request. VIDEO_MAX_BYTES is sized for ~10 min of 720p
# H.264 at the bitrate react-native-compressor produces (2.5 Mbps ≈
# 18 MB/min).
IMAGE_MAX_BYTES = 30 * 1024 * 1024          # 30 MB per image
VIDEO_MAX_BYTES = 250 * 1024 * 1024         # 250 MB per video

# Pillow's default MAX_IMAGE_PIXELS is ~89 Mpix and only logs a warning —
# we want it to RAISE before allocating the decoded buffer. 50 Mpix covers
# every consumer camera (a 200 MP Samsung sensor produces a 12 MP JPEG by
# default; raw 200 MP files won't be uploaded by our app).
Image.MAX_IMAGE_PIXELS = 50_000_000



FONTS_DIR = os.path.join(getattr(settings, 'BASE_DIR', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'fonts')

# family → weight → PostScript name (== filename without .ttf)
_BUNDLED_FONTS = {
    'Caveat': {
        '400': 'Caveat-Regular', '600': 'Caveat-SemiBold', '800': 'Caveat-Bold',
    },
    'Oswald': {
        '400': 'Oswald-Regular', '600': 'Oswald-SemiBold', '800': 'Oswald-Bold',
    },
    'Fredoka': {
        '400': 'Fredoka-Regular', '600': 'Fredoka-Medium', '800': 'Fredoka-Bold',
    },
    'Montserrat': {
        '400': 'Montserrat-Regular', '600': 'Montserrat-SemiBold', '800': 'Montserrat-ExtraBold',
    },
    'PlayfairDisplay': {
        '400': 'PlayfairDisplay-Regular', '600': 'PlayfairDisplay-SemiBold', '800': 'PlayfairDisplay-ExtraBold',
    },
    'JetBrainsMono': {
        '400': 'JetBrainsMono-Regular', '600': 'JetBrainsMono-SemiBold', '800': 'JetBrainsMono-ExtraBold',
    },
    'Nunito': {
        '400': 'Nunito-Regular', '600': 'Nunito-SemiBold', '800': 'Nunito-ExtraBold',
    },
}

# System-font fallbacks per weight, used for the "Default" / System option
# and whenever a bundled TTF is missing. Try several common Linux paths plus
# Windows for dev. First existing one wins.
_SYSTEM_FONT_CANDIDATES = {
    'bold': [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        'C:/Windows/Fonts/arialbd.ttf',
    ],
    'regular': [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        'C:/Windows/Fonts/arial.ttf',
    ],
}


# Magic-byte signatures we accept for "video/*" uploads. We sniff the first
# 32 bytes of the file rather than trusting the client's Content-Type header,
# which is freely spoofable. Covers MP4 / MOV / M4V (ISO base media, "ftyp"
# at offset 4), WebM / MKV (EBML), and AVI (RIFF). 3GP also uses ftyp so it
# falls under the MP4 branch.


def _sniff_video_signature(head: bytes) -> bool:
    if len(head) < 12:
        return False
    # ISO base media: 4-byte size, then 'ftyp', then 4-byte brand
    if head[4:8] == b'ftyp':
        return True
    # WebM / Matroska EBML header
    if head[:4] == b'\x1a\x45\xdf\xa3':
        return True
    # AVI: RIFF....AVI<space>
    if head[:4] == b'RIFF' and head[8:12] == b'AVI ':
        return True
    return False


def verify_uploaded_media(uploaded_file, *, claimed_kind: str) -> None:
    """
    Inspect file content to confirm it's actually an image / video before
    we accept it. Raises ValueError with a user-safe message on rejection.
    Leaves the file's read cursor at position 0 on success.

    `claimed_kind` is 'image' or 'video' — derived from the client's
    Content-Type header. We don't trust the header for storage decisions,
    but we use it to pick the right verifier.
    """
    if claimed_kind == 'image':
        # Pillow's verify() walks the file structure without fully decoding
        # the pixels, so it's cheap. It WILL raise DecompressionBombError
        # for oversize images thanks to our MAX_IMAGE_PIXELS setting above.
        try:
            uploaded_file.seek(0)
            with Image.open(uploaded_file) as probe:
                probe.verify()
        except Image.DecompressionBombError:
            raise ValueError('Image is too large to process.')
        except Exception as e:
            # Pillow raises a grab-bag of types (UnidentifiedImageError,
            # SyntaxError, OSError, …) for malformed files; collapse them
            # all to one user-visible message.
            raise ValueError('File is not a valid image.') from e
        finally:
            # Reset for downstream consumers (process_media_image or
            # PostMedia.file.save). verify() leaves the cursor mid-stream.
            uploaded_file.seek(0)
        return

    if claimed_kind == 'video':
        try:
            uploaded_file.seek(0)
            head = uploaded_file.read(32)
        finally:
            uploaded_file.seek(0)
        if not _sniff_video_signature(head):
            raise ValueError('File is not a recognised video format.')
        return

    raise ValueError(f'Unsupported media kind: {claimed_kind}')


def validate_image_upload(uploaded_file, *, max_bytes=IMAGE_MAX_BYTES):
    """
    Full validation for a single uploaded IMAGE — the avatar / profile-photo
    counterpart to the per-file checks create_post / create_comment run on post
    media, but image-only (avatars and page photos are never video).

    Raises ``ValueError`` with a user-safe message on the first problem, and
    returns ``None`` when the file is a safe, in-policy image. On success the
    read cursor is left at 0 (verify_uploaded_media guarantees this), so the
    caller can hand the file straight to ``ImageField.save()``.

    Centralised here so the avatar endpoints can't silently drift away from the
    hardened post/comment upload path again. ``ImageField`` assigned on a model
    and saved with a plain ``.save()`` does NOT run image validation (that only
    happens in ``full_clean()``), so without this an arbitrary client file would
    be written straight under ``media/avatars`` (the M2 finding; cf.
    UPLOAD_BUG_AUDIT.md).

    Checks, in order:
      1. the Content-Type header must be ``image/*`` — a cheap early reject;
         the header is only a hint and is re-checked against the bytes in (4);
      2. if the filename carries a recognised type, it must also be ``image/*``
         (catches an ``avatar.exe``-style content-type / extension mismatch);
      3. the size must be within ``max_bytes``;
      4. a magic-byte sniff via ``verify_uploaded_media(claimed_kind='image')``,
         which also trips Pillow's decompression-bomb guard.
    """
    client_ct = (getattr(uploaded_file, 'content_type', '') or '').lower()
    if not client_ct.startswith('image/'):
        raise ValueError(f'Unsupported file type: {client_ct or "unknown"}')

    name = getattr(uploaded_file, 'name', '') or ''
    guessed_ct = (mimetypes.guess_type(name)[0] or '').lower()
    if guessed_ct and not guessed_ct.startswith('image/'):
        raise ValueError(f'Content-type does not match filename: {name}')

    size = getattr(uploaded_file, 'size', None)
    if size is not None and size > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        raise ValueError(f'Image exceeds the {limit_mb} MB limit.')

    # Magic-byte sniff + decompression-bomb guard. Raises ValueError on
    # rejection and resets the cursor to 0 on success.
    verify_uploaded_media(uploaded_file, claimed_kind='image')


def _first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def resolve_overlay_font_path(font_family, font_weight):
    """
    Return an absolute path to the TTF that should render an overlay with
    the given (font_family, font_weight). Resolves bundled families first;
    falls back to a system font on miss.

    Mirrors `resolveTextWeight` + `resolvePostScriptName` on the client.
    """
    weight = str(font_weight) if font_weight is not None else '600'
    family = font_family or 'System'

    bundled = _BUNDLED_FONTS.get(family)
    if bundled:
        ps_name = bundled.get(weight) or bundled.get('400')
        if ps_name:
            candidate = os.path.join(FONTS_DIR, f'{ps_name}.ttf')
            if os.path.exists(candidate):
                return candidate
            # Bundled face missing on disk — fall through to system font.

    # System / unknown / missing bundled file: pick a system font that
    # roughly matches the requested weight so at least the bold/regular
    # distinction survives.
    bucket = 'bold' if int(weight) >= 700 else 'regular'
    return _first_existing(_SYSTEM_FONT_CANDIDATES[bucket]) \
        or _first_existing(_SYSTEM_FONT_CANDIDATES['regular'])


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


def _extract_first_frame_jpeg(video_path):
    """
    Grab the first frame of `video_path` and return it as a JPEG ContentFile.

    Used to derive a video's thumbnail from the *already-processed* clip (the
    one with the colour filter + text overlays baked in), so the thumbnail the
    feed shows is frame-for-frame identical to what plays. This is why we don't
    just run the still-image baker (process_media_image) on the client's raw
    thumbnail: the image baker approximates filters with Pillow ImageEnhance
    while the video baker uses FFmpeg's VIDEO_FILTER_CHAINS — the two produce
    visibly different looks, so the only way to guarantee the thumbnail matches
    the video is to pull an actual frame out of the finished video.

    Best-effort: returns None on any failure (probe/encode error, timeout,
    missing output). The caller falls back to the client-extracted raw-frame
    thumbnail in that case, so a flaky extraction never fails the upload.
    """
    THUMB_TIMEOUT = 30  # seconds — a single-frame grab is fast; cap defensively
    thumb_path = os.path.splitext(video_path)[0] + '_thumb.jpg'
    try:
        # -frames:v 1 grabs exactly one frame; -q:v 2 is a high-quality JPEG.
        # The processed video is already capped at 720px on its constraining
        # axis (the client compresses to maxSize 720 before upload), so we
        # don't resize here — the frame is already a sensible thumbnail size
        # and matches the video's resolution exactly.
        proc = (
            ffmpeg
            .input(video_path)
            .output(thumb_path, vframes=1, format='image2', vcodec='mjpeg',
                    **{'q:v': 2}, y=None)
            .run_async(quiet=True, pipe_stdout=True, pipe_stderr=True)
        )
        try:
            _, stderr = proc.communicate(timeout=THUMB_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            logger.warning('[_extract_first_frame_jpeg] timed out')
            return None

        if proc.returncode != 0 or not os.path.exists(thumb_path):
            err = (stderr or b'').decode('utf-8', errors='replace')
            logger.error(f'[_extract_first_frame_jpeg] ffmpeg failed: {err[-500:]}')
            return None

        with open(thumb_path, 'rb') as fh:
            return ContentFile(fh.read(), name='thumb.jpg')
    except Exception as e:
        logger.error(f'[_extract_first_frame_jpeg] unexpected: {e}')
        return None
    finally:
        try:
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
        except Exception:
            pass


def process_media_video(input_file, metadata):
    """
    Bakes filter + text overlays into a video using FFmpeg.
    Writes input to a temp file, runs FFmpeg, returns a tuple
    ``(video_content, thumb_content)``:

      * ``video_content`` — a ContentFile of the baked video.
      * ``thumb_content`` — a ContentFile of the baked video's first frame
        (so the thumbnail carries the same filter + overlays as the clip), or
        ``None`` if the best-effort frame grab failed. The caller decides what
        to do with a ``None`` thumbnail (it falls back to the client-extracted
        raw first frame).

    Raises on FFmpeg failure so the caller can roll back the post and surface
    a real error to the user, instead of silently saving the unedited video.
    """
    # Initialise here so the finally block can always reference them safely,
    # even if the NamedTemporaryFile creation fails before assignment.
    tmp_in_path = None
    tmp_out_path = None
    tmp_pill_path = None
    try:
        overlays = metadata.get('overlays', [])
        # CRITICAL: every numeric field below is interpolated into FFmpeg's
        # drawtext filtergraph as raw text. Without coercion a hostile client
        # can break out of the drawtext filter and inject sibling filters
        # (e.g. `movie`, which reads arbitrary files on most FFmpeg builds).
        # Coerce-and-bound every input here at the boundary so the only
        # strings that can ever reach FFmpeg are real numbers.
        filter_index = _safe_int(metadata.get('filter_index'), default=0, lo=0, hi=999)
        preview_w = _safe_float(metadata.get('preview_width'), default=1.0, lo=1.0, hi=100000.0)
        preview_h = _safe_float(metadata.get('preview_height'), default=1.0, lo=1.0, hi=100000.0)

        # Write uploaded video to a temp file so FFmpeg can read it
        suffix = os.path.splitext(input_file.name)[1] or '.mp4'
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_in:
            for chunk in input_file.chunks():
                tmp_in.write(chunk)
            tmp_in_path = tmp_in.name

        # Use splitext rather than .replace(suffix, ...), which would also
        # rewrite any other occurrence of the suffix inside the path.
        tmp_out_path = os.path.splitext(tmp_in_path)[0] + '_out.mp4'

        # ── Probe the video to get its concrete dimensions ───────────────
        # The image baker does all of its scaling in Python with concrete
        # numbers (`cover_scale = min(img_w/preview_w, img_h/preview_h)`,
        # `scaled_font_size = int(base*cover_scale)`, `pad = scaled*0.3`).
        # The video baker used to hand FFmpeg a runtime *expression*
        # (`fontsize=(20*min(main_w/W,main_h/H))`) and computed the box
        # padding in PREVIEW-space pixels, which left the embedded text and
        # its pill at different sizes from the image bake (the box border
        # in particular ended up tiny — a 20×0.2 = 4 px border around text
        # that had been scaled up ~3× to 56 px). To get true parity with
        # the image path we probe once, even-round the dimensions to match
        # the `scale=trunc(iw/2)*2` filter, and then run the SAME formulas
        # the image baker uses to produce concrete pixel sizes and offsets
        # for drawtext.
        vid_w = vid_h = None
        try:
            probe = ffmpeg.probe(tmp_in_path)
            vstream = next(
                s for s in probe.get('streams', [])
                if s.get('codec_type') == 'video'
            )
            raw_w = int(vstream.get('width') or 0)
            raw_h = int(vstream.get('height') or 0)
            # Honour rotation metadata: a phone-portrait video is usually
            # stored as e.g. 1920×1080 with rotate=90, but every player
            # shows it as 1080×1920. main_w/main_h after FFmpeg's auto-
            # rotation match the displayed dims, so we mirror that here.
            rotation = 0
            try:
                rotation = int(vstream.get('tags', {}).get('rotate', 0) or 0)
            except (TypeError, ValueError):
                pass
            for sd in vstream.get('side_data_list') or []:
                if sd.get('side_data_type') == 'Display Matrix':
                    try:
                        rotation = int(sd.get('rotation', 0) or 0)
                    except (TypeError, ValueError):
                        pass
            if abs(rotation) % 180 == 90:
                raw_w, raw_h = raw_h, raw_w
            # scale=trunc(iw/2)*2:trunc(ih/2)*2 runs ahead of drawtext, so
            # main_w/main_h inside the filter are even-rounded. Match that
            # here so our Python-side `cover_scale` agrees with FFmpeg.
            if raw_w >= 2 and raw_h >= 2:
                vid_w = raw_w - (raw_w % 2)
                vid_h = raw_h - (raw_h % 2)
        except Exception as probe_err:
            logger.warning(f'[process_media_video] probe failed; '
                  f'falling back to expression scaling: {probe_err}')

        if vid_w and vid_h:
            # MediaRenderer.tsx renders the video preview with
            # `resizeMode="contain"`, which means the video is scaled down
            # to fit inside the preview box, with letterboxing on whichever
            # axis doesn't match the preview's aspect. To map a preview-DIP
            # coordinate back onto the underlying video pixels we need the
            # *inverse* of that render scale, which is
            # `max(vid_w/preview_w, vid_h/preview_h)`.
            #
            # The variable was previously computed with `min(...)` (despite
            # being named `cover_scale` and a comment claiming "contain"),
            # which is the cover-mode inverse. For aspect-matching videos
            # min and max are equal, so the bug was invisible — but as soon
            # as the video's aspect differed from the preview's (square or
            # landscape video shot/uploaded into the 9:~17.6 portrait
            # preview), the min branch used the *smaller* of the two
            # ratios, scaling the embedded font size, pill padding, and
            # overlay position by roughly half what they should have been.
            # The finished video then showed text that was visibly much
            # smaller than the preview did.
            preview_scale = max(vid_w / preview_w, vid_h / preview_h)
            # One of these will be 0 (the axis whose ratio is the max);
            # the other will be negative, which correctly shifts the
            # overlay frame to account for the letterboxed axis.
            offset_x = (vid_w - preview_w * preview_scale) / 2
            offset_y = (vid_h - preview_h * preview_scale) / 2
        else:
            preview_scale = None
            offset_x = offset_y = 0.0

        # ── Build filter graph ───────────────────────────────────────────
        # Colour grade runs first; the rounded background pills and the text
        # are composited on top of the graded frame — mirroring the Pillow
        # image baker, which colour-filters the photo and *then* draws the
        # pills, so the pill/text themselves are never colour-graded.
        color_filter = VIDEO_FILTER_CHAINS.get(filter_index)

        # drawtext entries — ONLY used on the probe-failed fallback path (no
        # concrete video dims), where we can't build a full-frame Pillow layer.
        drawtext_parts = []

        # Overlays to render with Pillow (the probe-success path). We draw the
        # rounded pill AND the glyphs with Pillow — exactly like the image
        # baker, via the shared _draw_text_overlay helper — into one full-frame
        # transparent PNG, then composite it onto the video with FFmpeg's
        # overlay filter. Two reasons this beats FFmpeg's drawtext:
        #   1. drawtext's `box` can only draw a SHARP rectangle (no corner
        #      radius), so the baked video used to show square corners while
        #      the image baker showed rounded ones.
        #   2. drawtext and Pillow disagree on where `y` places the text, so a
        #      Pillow pill wrapped around drawtext glyphs left the text riding
        #      high. Drawing the text with Pillow too centres it perfectly,
        #      pixel-identical to a baked image.
        # Each entry carries the params _draw_text_overlay needs. Only
        # populated when the probe gave concrete dims; otherwise we fall back
        # to drawtext above so a failed probe still produces a valid encode.
        pil_overlays = []

        for ov in overlays:
            if not isinstance(ov, dict):
                # Hostile / malformed payload — skip rather than fail the
                # whole encode (which would roll back the user's post).
                continue
            text = ov.get('text', '')
            if not text:
                continue

            # Per-overlay font path so the baked video uses the same face
            # the user saw in the editor. Falls back to a system font when
            # the bundled TTF isn't present in backend/fonts/.
            font_path = resolve_overlay_font_path(
                ov.get('fontFamily'),
                ov.get('fontWeight'),
            )
            if not font_path:
                # No usable font at all — skip this overlay rather than
                # crashing the whole encode.
                logger.warning('[process_media_video] no font available; skipping overlay')
                continue

            # Escape FFmpeg drawtext special characters. The order matters:
            # backslash first so we don't double-escape the others.
            escaped = (text
                .replace('\\', '\\\\')
                .replace("'", "\\'")
                .replace(':', '\\:')
                .replace('%', '\\%')
                .replace('=', '\\=')
                .replace(',', '\\,'))

            # Coerced — this value is interpolated into the filtergraph
            # below. An unescaped string here is the primary injection
            # surface. Default 24 matches process_media_image so the image
            # and video bakers agree when the client omits fontSize.
            font_size = _safe_float(ov.get('fontSize'), default=24.0, lo=1.0, hi=1000.0)
            has_bg = bool(ov.get('hasBackground', True))

            # Pixel-accurate positioning: prefer the geometry the client
            # measured for this overlay in the live preview (captured via
            # <Text onTextLayout> in DraggableText.tsx and shipped alongside
            # the overlay payload as `measuredBaselineOffsetX/Y` and
            # `measuredAscender`). Using those means the embedded video text
            # sits at the same pixel RN drew it, instead of being inferred
            # from the constants + a font-table-derived ascent — which is
            # what was leaving a 1–2 px residual against the preview because
            # FFmpeg's drawtext, Skia, and RN's text engine each interpret
            # the .ttf's metric tables a touch differently.
            #
            # `drawtext`'s `y` is the top of the line box (not the baseline)
            # so we have to subtract the line's ascender to convert the
            # baseline the client gave us into a line-top. Fall back to the
            # constants + 0-ascent assumption (line-top == container/text
            # padding) if no measurement was captured — the same path the
            # previous version used, kept as a safety net for old clients
            # or fast uploads that fire before the first layout pass.
            CONTAINER_PAD = 10
            TEXT_PAD_X = 20
            TEXT_PAD_Y = 10
            # All four below are coerced. measured_* fall back to None on
            # missing/invalid so the legacy positioning branch still kicks
            # in for old clients that didn't ship the measurement payload.
            base_x = _safe_float(ov.get('x'), default=0.0)
            base_y = _safe_float(ov.get('y'), default=0.0)
            measured_off_x = _safe_optional_float(ov.get('measuredBaselineOffsetX'))
            measured_off_y = _safe_optional_float(ov.get('measuredBaselineOffsetY'))
            measured_ascender = _safe_optional_float(ov.get('measuredAscender'))
            if (
                measured_off_x is not None
                and measured_off_y is not None
                and measured_ascender is not None
            ):
                # Use exact client-measured baseline offsets; convert baseline
                # → line-top by subtracting the ascender (drawtext's `y` is
                # line-top).
                ov_x = base_x + measured_off_x
                ov_y = base_y + (measured_off_y - measured_ascender)
            else:
                ov_x = base_x + CONTAINER_PAD + TEXT_PAD_X
                ov_y = base_y + CONTAINER_PAD + TEXT_PAD_Y

            # Two paths, decided by whether the probe gave us concrete dims:
            #
            #  • Probe succeeded (`preview_scale` is a real number) — scale in
            #    Python with concrete numbers (exactly like process_media_image)
            #    and render this overlay with Pillow after the loop. Drawing the
            #    pill AND the glyphs with Pillow is what centres the text in the
            #    pill, pixel-identical to a baked image. We collect it and skip
            #    all the drawtext machinery below.
            #
            #  • Probe failed — fall back to FFmpeg drawtext with runtime
            #    expressions and a (sharp) box, so the encode still succeeds.
            if preview_scale is not None:
                scaled_font_size = max(1, int(round(font_size * preview_scale)))
                final_x = ov_x * preview_scale + offset_x
                final_y = ov_y * preview_scale + offset_y
                pil_overlays.append({
                    'text': text,
                    'font_path': font_path,
                    'scaled_font_size': scaled_font_size,
                    'final_x': final_x,
                    'final_y': final_y,
                    'has_background': has_bg,
                })
                continue

            # ── Probe-failed fallback: FFmpeg drawtext ─────────────────────
            # No concrete dims → we can't build a full-frame Pillow layer, so
            # position with runtime expressions. `,` is filtergraph-special;
            # escape it so the max() comma survives into drawtext's expression
            # evaluator. We use `max` (not `min`) because the preview renders
            # the video with `resizeMode="contain"` — see the `preview_scale`
            # comment above.
            scale_expr = f'max(main_w/{preview_w}\\,main_h/{preview_h})'
            fs = f'({font_size}*({scale_expr}))'
            x = f'({ov_x}*({scale_expr})+(main_w-{preview_w}*({scale_expr}))/2)'
            y = f'({ov_y}*({scale_expr})+(main_h-{preview_h}*({scale_expr}))/2)'

            # FFmpeg's drawtext parser uses `:` as the option separator, so
            # the colon in Windows paths like `C:/Windows/Fonts/arial.ttf`
            # must be escaped. There are TWO escaping levels for the value
            # to survive (see ffmpeg-utils(1) "Notes on filtergraph
            # escaping"):
            #
            #   1. Filtergraph level (the whole `-vf` string). Backslash is
            #      a literal-next-char escape here — `\X` becomes `X` and
            #      the backslash itself is consumed.
            #   2. Filter-options level (inside one filter's option list).
            #      Here `\:` escapes the option separator to a literal `:`.
            #
            # So to land a literal `:` in the drawtext value we have to
            # write `\\:` at the source: the filtergraph parser eats one
            # backslash and leaves `\:` for drawtext's option parser, which
            # then unescapes it to `:`. Writing only `\:` gets the
            # backslash eaten at level 1 and leaves a bare `:` for
            # drawtext — symptom:
            #   "No option name near '/Windows/Fonts/arial.ttf:fontsize=..."
            # Single quotes around the value don't help on the gyan.dev
            # Windows build, so we drop them and rely on the explicit
            # escape. Normalise backslashes to forward slashes first so we
            # don't have to escape those too.
            font_path_for_filter = (
                font_path
                .replace('\\', '/')
                .replace(':', r'\\:')
            )

            dt = (
                f"drawtext=text='{escaped}'"
                f":fontfile={font_path_for_filter}"
                f":fontsize={fs}"
                f":fontcolor=white"
                f":x={x}"
                f":y={y}"
            )

            if has_bg:
                # Probe-failed fallback only: a SHARP drawtext box. (The
                # rounded, centred pill is drawn with Pillow on the success
                # path above.) `boxborderw` doesn't accept expressions, so we
                # pad in preview-space DIP — mirrors DraggableText.tsx's
                # paddingHorizontal: 20 / paddingVertical: 10.
                PREVIEW_TEXT_PAD_X = 20
                PREVIEW_TEXT_PAD_Y = 10
                dt += (
                    f":box=1:boxcolor=black@0.5"
                    f":boxborderw={PREVIEW_TEXT_PAD_Y}|{PREVIEW_TEXT_PAD_X}|{PREVIEW_TEXT_PAD_Y}"
                )
            else:
                dt += ":shadowcolor=black@0.8:shadowx=2:shadowy=2"

            drawtext_parts.append(dt)

        # ── Run FFmpeg ───────────────────────────────────────────────────
        # `ffmpeg-python`'s `.run()` doesn't expose a timeout, so we go via
        # `.run_async()` + `Popen.communicate(timeout=...)` to make sure a
        # pathologically slow/malicious video can't tie up a request worker
        # indefinitely. On timeout we kill the process and raise.
        FFMPEG_TIMEOUT = 180  # seconds

        # ── Render the overlay layer (pills + glyphs) into one PNG ──────
        # Probe-success path: every overlay was collected in `pil_overlays`.
        # Draw them all with Pillow — via the shared _draw_text_overlay helper,
        # the SAME code the image baker uses — into a single full-frame
        # transparent PNG at the video's concrete, even-rounded dimensions.
        # Because the glyphs are drawn with Pillow (not FFmpeg drawtext), they
        # land dead-centre in the rounded pill, identical to a baked image. The
        # PNG is composited onto the colour-graded frame below.
        if pil_overlays and vid_w and vid_h:
            overlay_layer = Image.new('RGBA', (vid_w, vid_h), (0, 0, 0, 0))
            layer_draw = ImageDraw.Draw(overlay_layer)
            for po in pil_overlays:
                # Load the user's selected face at the scaled size; fall back
                # to Pillow's default on any failure (mirrors the image baker).
                try:
                    if po['font_path']:
                        po_font = ImageFont.truetype(
                            po['font_path'], po['scaled_font_size'])
                    else:
                        po_font = ImageFont.load_default()
                except Exception:
                    po_font = ImageFont.load_default()
                _draw_text_overlay(
                    layer_draw, po['text'], po_font,
                    po['final_x'], po['final_y'], preview_scale,
                    po['has_background'],
                )
            tmp_pill_path = os.path.splitext(tmp_in_path)[0] + '_overlay.png'
            overlay_layer.save(tmp_pill_path)

        if color_filter or drawtext_parts or tmp_pill_path:
            # Build the filter chain we'll hand to libx264:
            #
            # 1. `scale=trunc(iw/2)*2:trunc(ih/2)*2` — many phone cameras
            #    produce odd-width video (e.g. 405×720 from some Android
            #    devices). libx264 + yuv420p requires BOTH dimensions to be
            #    even, otherwise it refuses to open with "width not divisible
            #    by 2". Must be the FIRST filter so `main_w`/`main_h` (and the
            #    full-frame overlay PNG sized to vid_w/vid_h) line up with the
            #    corrected dims.
            #
            # 2. Colour filter, then — if present — the Pillow overlay layer
            #    (pills + glyphs) composited via `overlay`. Any drawtext entries
            #    (probe-failed fallback only) go on top of that.
            #
            # 3. `format=yuv420p` at the tail — the filter graph can promote
            #    chroma, which makes libx264 pick High 4:4:4 Predictive; most
            #    consumer players only support 8-bit 4:2:0 H.264. Forcing
            #    yuv420p makes libx264 pick the standard High profile, which
            #    plays everywhere.
            base_parts = ['scale=trunc(iw/2)*2:trunc(ih/2)*2']
            if color_filter:
                base_parts.append(color_filter)  # colour grade before overlay

            if tmp_pill_path:
                # `movie` injects the overlay PNG as a second source INSIDE the
                # simple -vf graph, so we don't need an extra `-i` input. It's
                # overlaid AFTER the colour filter (so the overlay isn't graded)
                # and BEFORE any drawtext. Escape the path the same way the font
                # path is escaped: normalise backslashes and escape the
                # option-separator colon (matters for Windows drive letters;
                # production tmp paths are colon-free).
                overlay_for_filter = (
                    tmp_pill_path.replace('\\', '/').replace(':', r'\\:')
                )
                base_chain = ','.join(base_parts)
                post_chain = ','.join(list(drawtext_parts) + ['format=yuv420p'])
                vf_value = (
                    f"movie={overlay_for_filter}[ov];"
                    f"[in]{base_chain}[base];"
                    f"[base][ov]overlay=0:0[bg];"
                    f"[bg]{post_chain}[out]"
                )
            else:
                vf_value = ','.join(
                    base_parts + list(drawtext_parts) + ['format=yuv420p']
                )

            ff_process = (
                ffmpeg
                .input(tmp_in_path)
                .output(
                    tmp_out_path,
                    vf=vf_value,
                    vcodec='libx264',
                    crf=23,
                    preset='fast',
                    acodec='copy',
                    movflags='+faststart',
                    y=None,
                )
                .run_async(quiet=True, pipe_stdout=True, pipe_stderr=True)
            )
        else:
            ff_process = (
                ffmpeg
                .input(tmp_in_path)
                .output(tmp_out_path, vcodec='copy', acodec='copy', y=None)
                .run_async(quiet=True, pipe_stdout=True, pipe_stderr=True)
            )

        try:
            _, stderr = ff_process.communicate(timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired:
            ff_process.kill()
            ff_process.communicate()
            raise RuntimeError(f'FFmpeg timed out after {FFMPEG_TIMEOUT}s')

        if ff_process.returncode != 0:
            full_err = (stderr or b'').decode('utf-8', errors='replace')
            # Print the full stderr to the server log for diagnostics — the
            # truncated version below loses the lines that actually explain
            # the failure (libx264 prints its real complaint several lines
            # before the "Could not open encoder before EOF" line).
            logger.error('[process_media_video] FFmpeg stderr (full):')
            logger.error(full_err)
            raise RuntimeError(
                f'FFmpeg failed (exit {ff_process.returncode}): '
                f'{full_err[-2000:]}'
            )

        # Read output and return as ContentFile
        with open(tmp_out_path, 'rb') as f:
            content = ContentFile(f.read(), name=input_file.name)

        # Derive the thumbnail from the FINISHED video's first frame so it
        # carries the exact same colour filter and text overlays as the
        # published clip. We do this here, while tmp_out_path is still on
        # disk, to avoid a second decode of the (potentially large) video
        # bytes. Best-effort — thumb_content is None on failure and the
        # caller falls back to the client-extracted raw-frame thumbnail.
        thumb_content = _extract_first_frame_jpeg(tmp_out_path)

        return content, thumb_content

    finally:
        # Always clean up temp files (paths may be None if setup failed)
        for path in [tmp_in_path, tmp_out_path, tmp_pill_path]:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
