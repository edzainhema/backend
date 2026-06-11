"""Adaptive-bitrate HLS packaging for short videos (reels / feed).

Given a processed clip's bytes, produce an HLS **VOD** ladder — a master
playlist plus one variant playlist + TS segments per rendition — so the app
streams adaptive bitrate (instant low-rung start, silent upgrade as the buffer
fills) instead of progressively downloading a single MP4.

This is layered ON TOP of the existing single-MP4 pipeline, not a replacement:
``PostMedia.file`` stays the MP4 and ``hls_master`` points at the master
playlist. Everything here is best-effort — ``build_hls_ladder`` returns
``None`` on any failure (probe/encode error, timeout, missing ffmpeg binary),
and the caller keeps the MP4 so an HLS failure can never fail an upload.
"""
import logging
import os
import subprocess
import tempfile
import uuid

import ffmpeg
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

# Rendition ladder, tuned for vertical phone reels. `size` is the cap on the
# LONGER edge in pixels. The client compresses to ~720px on the longer edge
# before upload, so 720 is the ceiling — higher rungs would just upscale and
# waste storage/egress (the cost the ladder is meant to keep down).
#
# Two-rung 360/720 ladder (dropped the previous 540p middle rung) — on phone-
# first social video the player smoothly adapts between 360 and 720 without
# needing the intermediate tier, and removing one rung cuts encode time and
# storage by ~33%. Cost: viewers on the narrow bandwidth band that could have
# used 540p (poor 3G / congested wifi) get locked at 360p one notch lower than
# they otherwise would — affects a small minority of sessions. If we ever see
# real-world complaints about quality on those networks, add the 540p entry
# back here and that's the only change needed.
HLS_LADDER = (
    {"name": "360", "size": 360, "v_kbps": 500,  "maxrate_kbps": 540,  "bufsize_kbps": 750},
    {"name": "720", "size": 720, "v_kbps": 2000, "maxrate_kbps": 2200, "bufsize_kbps": 3000},
)
AUDIO_KBPS = 128
SEGMENT_SECONDS = 4

# x264 speed/efficiency preset. `veryfast` encodes ~2-3x quicker than `fast`
# for a negligible quality difference at these small reel resolutions — the
# difference between a long clip finishing comfortably vs timing out. Tunable
# via env so a host with spare CPU (or a need for smaller files) can dial it
# back toward `fast`/`medium`.
X264_PRESET = os.environ.get("HLS_X264_PRESET", "veryfast")

# Wall-clock cap on a single ladder encode. Sized for LONG clips (the app allows
# up to ~7-minute videos, each encoded into 3 renditions), so the default is
# generous. The process_post_media Celery task gives itself soft/hard time
# limits ABOVE this (see tasks.py) so ffmpeg's own timeout fires first with a
# clean log line, rather than the worker raising SoftTimeLimitExceeded mid-
# encode. Tune to your hardware: measure a worst-case (7-min) encode on the
# real server and set this to ~2x that.
HLS_TIMEOUT = int(os.environ.get("HLS_ENCODE_TIMEOUT", "900"))
MASTER_NAME = "master.m3u8"

# Allow ops to point at a specific binary; defaults to PATH lookup.
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")


# Max portrait aspect for the streamed renditions: 9:16 (width:height). A clip
# taller than this gets its top+bottom cropped before the ABR split, so the HLS
# stream matches the 9:16 stored thumbnail/dims and the feed's render-time cap.
# Kept in sync with images.MAX_PORTRAIT_RATIO (imported lazily to avoid a cycle).
MAX_PORTRAIT_RATIO = 16 / 9


def _displayed_dimensions(path):
    """Probe ``path`` for the video's DISPLAYED (rotation-corrected, even)
    dimensions, returning ``(w, h)`` or ``None`` on any failure.

    Mirrors the rotation handling in ``process_media_video``: a phone-portrait
    clip is often stored as landscape pixels + a rotate/Display-Matrix tag, but
    every player (and ffmpeg's autorotate, which runs before the filtergraph)
    shows it rotated — so the crop must be computed against the rotated dims.
    Even-rounded because libx264 + yuv420p require even dimensions.
    """
    try:
        probe = ffmpeg.probe(path)
        vstream = next(
            s for s in probe.get("streams", []) if s.get("codec_type") == "video"
        )
        w = int(vstream.get("width") or 0)
        h = int(vstream.get("height") or 0)
        rotation = 0
        try:
            rotation = int(vstream.get("tags", {}).get("rotate", 0) or 0)
        except (TypeError, ValueError):
            pass
        for sd in vstream.get("side_data_list") or []:
            if sd.get("side_data_type") == "Display Matrix":
                try:
                    rotation = int(sd.get("rotation", 0) or 0)
                except (TypeError, ValueError):
                    pass
        if abs(rotation) % 180 == 90:
            w, h = h, w
        if w < 2 or h < 2:
            return None
        return w - (w % 2), h - (h % 2)
    except Exception as e:
        logger.warning(f"[hls] dimension probe failed; no crop applied: {e}")
        return None


def _crop_filter(w, h, max_ratio=MAX_PORTRAIT_RATIO):
    """Return a concrete ``crop=W:H:0:Y`` filter that trims a too-tall clip's
    top+bottom down to ``max_ratio`` (height/width), or ``None`` when the clip
    is already within the cap (no crop needed). Concrete integers only — no
    runtime expressions — so it's safe in the argv with no filtergraph escaping.
    """
    if not w or not h:
        return None
    max_h = int(round(w * max_ratio))
    max_h -= max_h % 2  # keep even for yuv420p
    if max_h <= 0 or h <= max_h:
        return None
    y = (h - max_h) // 2
    return f"crop={w}:{max_h}:0:{y}"


def _scale_filter(target):
    """Scale filter that caps the LONGER edge at ``target`` px, preserves
    aspect, and keeps BOTH dimensions even (libx264 + yuv420p require even
    dims). Orientation-agnostic: landscape → width=target, portrait →
    height=target; the other axis is ``-2`` (auto, rounded to even)."""
    return (
        f"scale=w='if(gte(iw,ih),{target},-2)':"
        f"h='if(gte(iw,ih),-2,{target})'"
    )


def build_filter_complex(ladder=HLS_LADDER, crop=None):
    """Build the ``-filter_complex`` value: optionally crop the source to the
    9:16 cap, then split into N branches and scale each to its rung.
    Pure/deterministic — exported for tests.

    ``crop`` is a concrete ``crop=W:H:x:y`` filter string (from ``_crop_filter``)
    or ``None``. When present it runs FIRST on ``[0:v]``, before the split, so a
    single crop feeds every rung. Produces output labels ``[v0out]``, ``[v1out]``,
    … one per rung.
    """
    n = len(ladder)
    splits = "".join(f"[v{i}]" for i in range(n))
    pre = f"{crop}," if crop else ""
    parts = [f"[0:v]{pre}split={n}{splits}"]
    for i, rung in enumerate(ladder):
        parts.append(f"[v{i}]{_scale_filter(rung['size'])}[v{i}out]")
    return ";".join(parts)


def build_var_stream_map(n, has_audio):
    """HLS ``-var_stream_map``: one entry per rung. Each variant gets its own
    audio rendition when the source has audio. Pure — exported for tests."""
    if has_audio:
        return " ".join(f"v:{i},a:{i}" for i in range(n))
    return " ".join(f"v:{i}" for i in range(n))


def build_hls_ffmpeg_args(input_path, out_dir, has_audio, ladder=HLS_LADDER, crop=None):
    """Assemble the full ffmpeg argv (after the binary name) for the ladder.

    Pure and deterministic given its inputs — exported so the command can be
    unit-tested without invoking ffmpeg. ``crop`` (a concrete ``crop=...`` filter
    string or ``None``) is threaded into the filtergraph to bake the 9:16 cap.
    """
    n = len(ladder)
    args = ["-y", "-i", input_path, "-filter_complex", build_filter_complex(ladder, crop)]

    for i, rung in enumerate(ladder):
        args += [
            "-map", f"[v{i}out]",
            f"-c:v:{i}", "libx264",
            "-preset", X264_PRESET,
            f"-b:v:{i}", f"{rung['v_kbps']}k",
            f"-maxrate:v:{i}", f"{rung['maxrate_kbps']}k",
            f"-bufsize:v:{i}", f"{rung['bufsize_kbps']}k",
        ]

    if has_audio:
        # Map the source audio once per variant so each rung carries its own
        # AAC track (var_stream_map pairs v:i with a:i).
        for _ in range(n):
            args += ["-map", "a:0"]
        args += ["-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ac", "2"]

    args += [
        # Force regular keyframes so segments are aligned across renditions —
        # required for seamless ABR switching. sc_threshold=0 stops the encoder
        # inserting extra keyframes on scene cuts (which would misalign them).
        "-g", "48", "-keyint_min", "48", "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", os.path.join(out_dir, "v%v_%03d.ts"),
        "-master_pl_name", MASTER_NAME,
        "-var_stream_map", build_var_stream_map(n, has_audio),
        os.path.join(out_dir, "v%v.m3u8"),
    ]
    return args


def _has_audio_stream(path):
    """True if the file has at least one audio stream. Best-effort: on a probe
    failure we assume no audio (so we never request an a:0 map that ffmpeg
    would reject, which would fail the whole ladder)."""
    try:
        probe = ffmpeg.probe(path)
        return any(
            s.get("codec_type") == "audio" for s in probe.get("streams", [])
        )
    except Exception as e:
        logger.warning(f"[hls] audio probe failed, assuming none: {e}")
        return False


def build_hls_ladder(video_bytes, ladder=HLS_LADDER):
    """Package ``video_bytes`` into an HLS ladder.

    Returns ``(master_name, files)`` where ``files`` is a list of
    ``(relative_name, bytes)`` for every artifact (master playlist, variant
    playlists, segments) — all with flat, relative names so they can be stored
    under one prefix and resolve against the master's URL. Returns ``None`` on
    any failure (caller falls back to the plain MP4).
    """
    tmp_in = None
    tmp_dir = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            fh.write(video_bytes)
            tmp_in = fh.name

        tmp_dir = tempfile.mkdtemp(prefix="hls_")
        has_audio = _has_audio_stream(tmp_in)
        # Bake the 9:16 portrait cap into the renditions when the source is
        # taller (best-effort: a probe failure → no crop, just like the MP4).
        dims = _displayed_dimensions(tmp_in)
        crop = _crop_filter(*dims) if dims else None
        args = [FFMPEG_BIN] + build_hls_ffmpeg_args(tmp_in, tmp_dir, has_audio, ladder, crop)

        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            _, stderr = proc.communicate(timeout=HLS_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            logger.error(f"[hls] ffmpeg timed out after {HLS_TIMEOUT}s")
            return None

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace")
            logger.error(f"[hls] ffmpeg failed (exit {proc.returncode}): {err[-2000:]}")
            return None

        master_path = os.path.join(tmp_dir, MASTER_NAME)
        if not os.path.exists(master_path):
            logger.error("[hls] master playlist missing after encode")
            return None

        files = []
        for name in sorted(os.listdir(tmp_dir)):
            full = os.path.join(tmp_dir, name)
            if not os.path.isfile(full):
                continue
            with open(full, "rb") as f:
                files.append((name, f.read()))

        if not files:
            return None
        return MASTER_NAME, files

    except Exception as e:
        logger.error(f"[hls] unexpected failure: {e}")
        return None
    finally:
        try:
            if tmp_in and os.path.exists(tmp_in):
                os.remove(tmp_in)
        except Exception:
            pass
        if tmp_dir and os.path.isdir(tmp_dir):
            for name in os.listdir(tmp_dir):
                try:
                    os.remove(os.path.join(tmp_dir, name))
                except Exception:
                    pass
            try:
                os.rmdir(tmp_dir)
            except Exception:
                pass


def store_hls_bundle(bundle):
    """Upload an HLS bundle (master playlist + variant playlists + segments) to
    the configured storage under a single unique prefix, so the playlists'
    relative references resolve against the master's URL.

    ``bundle`` is ``(master_name, [(relative_name, bytes), ...])`` as returned
    by ``build_hls_ladder``. Returns ``(master_key, all_keys)`` where
    ``master_key`` is the storage key of the master playlist (assign it to
    ``PostMedia.hls_master.name``) and ``all_keys`` is every key written (so the
    caller can register them for cleanup).

    On any failure mid-upload, deletes the partial writes and re-raises — the
    caller treats that as "no HLS" and keeps the MP4.
    """
    master_name, files = bundle
    # A fresh uuid prefix guarantees no key collision, so default_storage.save
    # returns the exact key we pass (no random suffix that would break the
    # playlists' relative segment references).
    prefix = f"hls/{uuid.uuid4().hex}"
    written = []
    master_key = None
    try:
        for name, data in files:
            saved = default_storage.save(f"{prefix}/{name}", ContentFile(data))
            written.append(saved)
            if name == master_name:
                master_key = saved
        if not master_key:
            raise RuntimeError("HLS bundle missing its master playlist")
        return master_key, written
    except Exception:
        for key in written:
            try:
                default_storage.delete(key)
            except Exception:
                pass
        raise
