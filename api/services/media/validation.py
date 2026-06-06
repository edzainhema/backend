"""Upload safety: size caps, magic-byte sniffing, decompression-bomb guard.
Run before we ever touch the bytes a client uploaded."""
import mimetypes
import warnings

from PIL import Image

# ── Upload safety limits ────────────────────────────────────────────────────
# These caps protect the server from cheap DoS via oversized uploads and
# decompression bombs. Keep IMAGE_MAX_BYTES generous enough for a high-res
# phone JPEG (~10 MB on iPhone) but tight enough that an attacker can't fill
# the disk with one request. VIDEO_MAX_BYTES is sized for ~10 min of 720p
# H.264 at the bitrate react-native-compressor produces (2.5 Mbps ≈
# 18 MB/min). AUDIO_MAX_BYTES is sized for voice messages (a 10-minute
# AAC voice note at 64 kbps is ~5 MB; 50 MB is comfortably above that).
IMAGE_MAX_BYTES = 30 * 1024 * 1024          # 30 MB per image
VIDEO_MAX_BYTES = 250 * 1024 * 1024         # 250 MB per video
AUDIO_MAX_BYTES = 50 * 1024 * 1024          # 50 MB per audio clip

# ── Decompression-bomb guard ────────────────────────────────────────────────
# A decompression bomb is a TINY file (a few hundred KB) whose pixels expand to
# hundreds of megapixels on decode. It sails past IMAGE_MAX_BYTES — a byte cap
# can't see decoded dimensions — and then spikes memory/CPU when Pillow finally
# decodes it (process_media_image / ImageField.save). The pixel ceiling is
# Pillow's `Image.MAX_IMAGE_PIXELS`, but the library never sets it to anything
# WE chose: its default (~89 MP) only *warns* at the threshold and only raises
# the hard `DecompressionBombError` at 2× it, so by default everything up to
# ~179 MP slips through verify(). We pin an explicit value here AND promote the
# warning to an error inside `verify_uploaded_media`, so rejection lands at
# exactly this threshold instead of 2× it.
#
# 50 MP comfortably covers real cameras (12 MP typical; 48 MP full-res mode on
# modern flagships) while blocking the 100 MP+ range only a bomb or an absurd
# upload reaches. Peak decode is bounded at ~50M × 4 ≈ 200 MB RGBA. A deliberate
# 108 MP full-res shot (rare, opt-in, and far larger than a social feed needs)
# is rejected — shoot in normal mode. Tune here if your real max differs.
IMAGE_MAX_PIXELS = 50_000_000  # ~50 MP

# Apply process-wide at import. PIL.Image is a shared singleton, so this caps
# EVERY Pillow open in the process (this validator, process_media_image,
# overlays, avatars) — not only the calls in this module. api/apps.py:ready()
# re-applies it at Django startup so the ceiling is in force before the first
# request even if nothing has imported this module yet.
Image.MAX_IMAGE_PIXELS = IMAGE_MAX_PIXELS

# Default per-kind caps used by `validate_uploaded_media_file` when the caller
# doesn't pass an explicit override. Tunable per-callsite for endpoints that
# want stricter limits (e.g. comment attachments could cap images at 10 MB).
DEFAULT_MAX_BYTES_BY_KIND = {
    'image': IMAGE_MAX_BYTES,
    'video': VIDEO_MAX_BYTES,
    'audio': AUDIO_MAX_BYTES,
}


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


def _sniff_audio_signature(head: bytes) -> bool:
    """Recognise the audio formats the mobile client can produce.

    Voice notes on iOS land as `audio/m4a` (an ISO-BMFF container with an
    `ftyp` brand of `M4A `). Android's MediaRecorder writes to MP4
    containers as well (same `ftyp` magic, different brand). Web clients
    typically send ogg / opus or webm. We accept the union to keep the
    upload contract flexible without trusting the client's MIME header.

    Magic byte references:
      - ISO-BMFF (m4a, mp4-audio):  bytes 4..7 == 'ftyp'
      - Ogg (Vorbis, Opus, FLAC):    bytes 0..3 == 'OggS'
      - WebM / Matroska (web Opus):  bytes 0..3 == EBML header
      - WAVE:                        bytes 0..3 == 'RIFF', 8..11 == 'WAVE'
      - MP3 with ID3v2 tag:          bytes 0..2 == 'ID3'
      - MP3 raw (no ID3) frame sync: bytes 0..1 == 0xFF and 0xE0 mask match
      - FLAC native (not in Ogg):    bytes 0..3 == 'fLaC'

    The MP3 frame-sync check tolerates the various 0xFF Ex / 0xFF Fx
    variants (MPEG version 2 / 2.5 / 1, layer 1/2/3) by masking the
    second byte to its `1110_0000` bits.
    """
    if len(head) < 4:
        return False
    # ISO-BMFF (m4a, mp4 audio share the same magic as video; the audio
    # path here is intentionally permissive because the surrounding
    # `claimed_kind` discriminator filters out video bytes that happen to
    # share an ftyp header).
    if len(head) >= 8 and head[4:8] == b'ftyp':
        return True
    if head[:4] == b'OggS':
        return True
    if head[:4] == b'\x1a\x45\xdf\xa3':
        return True
    if head[:4] == b'fLaC':
        return True
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WAVE':
        return True
    if head[:3] == b'ID3':
        return True
    if (
        len(head) >= 2
        and head[0] == 0xFF
        and (head[1] & 0xE0) == 0xE0
    ):
        return True
    return False


def verify_uploaded_media(uploaded_file, *, claimed_kind: str) -> None:
    """
    Inspect file content to confirm it's actually an image / video / audio
    before we accept it. Raises ValueError with a user-safe message on
    rejection. Leaves the file's read cursor at position 0 on success.

    `claimed_kind` is 'image', 'video', or 'audio' — derived from the
    client's Content-Type header. We don't trust the header for storage
    decisions, but we use it to pick the right verifier.
    """
    if claimed_kind == 'image':
        # Pillow's verify() walks the file structure without fully decoding
        # the pixels, so it's cheap. Opening the image runs Pillow's
        # decompression-bomb check against IMAGE_MAX_PIXELS (set at module
        # load above):
        #   - over IMAGE_MAX_PIXELS      -> DecompressionBombWarning
        #   - over 2 * IMAGE_MAX_PIXELS  -> DecompressionBombError (always raised)
        # We promote the WARNING to an exception for the duration of this
        # check so rejection happens at exactly IMAGE_MAX_PIXELS, not at 2x it.
        # catch_warnings() scopes the filter to this block, so we never alter
        # global warning behaviour for the rest of the app.
        try:
            uploaded_file.seek(0)
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(uploaded_file) as probe:
                    probe.verify()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
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

    if claimed_kind == 'audio':
        try:
            uploaded_file.seek(0)
            head = uploaded_file.read(32)
        finally:
            uploaded_file.seek(0)
        if not _sniff_audio_signature(head):
            raise ValueError('File is not a recognised audio format.')
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


_IMAGE_FORMAT_TO_EXT = {
    'JPEG': '.jpg',
    'JPG':  '.jpg',
    'PNG':  '.png',
    'GIF':  '.gif',
    'WEBP': '.webp',
    'BMP':  '.bmp',
    'TIFF': '.tiff',
}


def safe_image_filename(uploaded_file, prefix: str) -> str:
    """Build a SERVER-derived filename for a validated image upload
    (audit M6).

    `prefix` should encode the owning entity, e.g.
    ``f"profile_{user.id}_avatar"`` or ``f"page_{page.id}_avatar"``.
    The extension is derived from the file's REAL bytes via a Pillow
    probe -- not from the client-supplied filename -- so an
    ``avatar.png`` upload that's actually JPEG bytes lands as
    ``profile_42_avatar.jpg``, never ``profile_42_avatar.png``.

    The pre-M6 avatar path did ``profile.avatar = uploaded_file`` and
    let Django's FileField persist the client's filename verbatim, so
    URLs ended up like ``/media/avatars/my-cat-IMG_1234.png`` --
    leaking the client's chosen string into the public URL. The bytes
    were already validated; this just removes the gratuitous trust on
    the *name*.

    Falls back to ``.jpg`` on any read / decode failure: that path is
    unreachable in normal use (validate_image_upload runs first and
    would have raised), but a corrupt-but-passes-verify edge case
    must not crash here -- a wrong-but-plausible extension is harmless,
    a 500 isn't. Cursor is reset to 0 on the way out so the caller
    can hand the same file object straight to ``FileField.save``.
    """
    from PIL import Image  # local import to keep startup light
    ext = '.jpg'
    try:
        uploaded_file.seek(0)
        with Image.open(uploaded_file) as probe:
            fmt = (probe.format or '').upper()
            ext = _IMAGE_FORMAT_TO_EXT.get(fmt, '.jpg')
    except Exception:
        pass
    finally:
        try:
            uploaded_file.seek(0)
        except Exception:
            pass
    return f'{prefix}{ext}'


def validate_uploaded_media_file(
    uploaded_file,
    *,
    allow_kinds=('image', 'video'),
    max_bytes_by_kind=None,
):
    """
    Single chokepoint for ALL non-avatar media uploads: post media,
    comment attachments, DM attachments, page-chat attachments.

    Audit B5: before this existed, the DM (``views/messaging/messages.py``)
    and page-chat (``views/page_chat.py``) send paths only inspected the
    client-supplied Content-Type header to classify the file, then saved
    the bytes verbatim. That accepted unbounded files, magic-byte spoofs,
    Pillow decompression bombs, and arbitrary executables labelled
    ``image/png``. Post / comment upload had hardened validation; the
    chat paths had not. This function is what they all share now.

    Returns the detected kind ('image' | 'video' | 'audio') on success
    so the caller can persist it directly to ``Message.media_type`` /
    ``PageChatMessage.media_type`` without re-detecting. Raises
    ``ValueError`` with a user-safe message on the first problem, and
    leaves the file's read cursor at 0 on success so the caller can
    hand it straight to ``FileField.save()`` / ``MessageMedia.file=``.

    Checks, in order, for each input:
      1. the Content-Type header must start with one of the allowed
         prefixes ('image/', 'video/', 'audio/' depending on
         ``allow_kinds``) — a cheap early reject;
      2. if the filename carries a recognised type, it must agree with
         the Content-Type's top-level (catches `voice.exe` with
         `Content-Type: audio/m4a`);
      3. the size must be within the per-kind cap (overridable via
         ``max_bytes_by_kind`` — defaults to ``DEFAULT_MAX_BYTES_BY_KIND``,
         which is the same `IMAGE_MAX_BYTES` / `VIDEO_MAX_BYTES` /
         `AUDIO_MAX_BYTES` constants used elsewhere);
      4. a magic-byte sniff via ``verify_uploaded_media``, which also
         trips Pillow's decompression-bomb guard for images and walks
         the audio / video signatures the helpers above recognise.

    `allow_kinds` lets each endpoint declare what it accepts:
      - post media: ('image', 'video')
      - comment attachments: ('image', 'video')
      - DM attachments: ('image', 'video', 'audio')
      - page-chat attachments: ('image', 'video', 'audio')

    Anything not in `allow_kinds` is rejected at step 1, BEFORE we read
    the body, so an attacker can't probe what's installed.
    """
    if max_bytes_by_kind is None:
        max_bytes_by_kind = DEFAULT_MAX_BYTES_BY_KIND

    client_ct = (getattr(uploaded_file, 'content_type', '') or '').lower()
    name = getattr(uploaded_file, 'name', '') or ''

    # Step 1: classify by Content-Type's top-level. Reject anything not
    # in the caller's allow_kinds list. This is also where we map header
    # -> our internal `kind` keyword.
    kind = None
    for k in ('image', 'video', 'audio'):
        if k in allow_kinds and client_ct.startswith(f'{k}/'):
            kind = k
            break
    if kind is None:
        # Don't echo the client_ct verbatim if it's empty -- "Unsupported
        # file type: " reads worse than the explicit "unknown".
        raise ValueError(
            f'Unsupported file type: {client_ct or "unknown"}'
        )

    # Step 2: if the filename carries a hint, it must agree with the
    # header's top-level. Catches `voice.exe` posted as `audio/m4a`.
    guessed_ct = (mimetypes.guess_type(name)[0] or '').lower()
    if guessed_ct and not guessed_ct.startswith(client_ct.split('/')[0]):
        raise ValueError(f'Content-type does not match filename: {name}')

    # Step 3: size cap, per kind. `f.size` is set by Django's upload
    # handler from the multipart length, so this rejects BEFORE we
    # spool the whole file to disk or run any decoders on it.
    max_bytes = max_bytes_by_kind.get(kind)
    size = getattr(uploaded_file, 'size', None)
    if max_bytes is not None and size is not None and size > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        raise ValueError(f'{kind.capitalize()} exceeds the {limit_mb} MB limit.')

    # Step 4: magic-byte sniff (+ decompression-bomb guard for images).
    # Resets the cursor to 0 on success.
    verify_uploaded_media(uploaded_file, claimed_kind=kind)
    return kind
