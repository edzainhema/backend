"""Video processing pipeline: ffmpeg transcode with filter chains, first-
frame thumbnail extraction, and text-overlay rendering on video frames."""
import logging
import os
import subprocess
import tempfile

import ffmpeg
from django.core.files.base import ContentFile
from PIL import Image, ImageDraw, ImageFont

from .fonts import resolve_overlay_font_path
from .filters import VIDEO_FILTER_CHAINS
from .overlays import (
    _safe_float, _safe_int, _safe_optional_float, _draw_text_overlay,
)

logger = logging.getLogger(__name__)


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
