"""Upload safety: size caps, magic-byte sniffing, decompression-bomb guard.
Run before we ever touch the bytes a client uploaded."""
import mimetypes

from PIL import Image

# ── Upload safety limits ────────────────────────────────────────────────────
# These caps protect the server from cheap DoS via oversized uploads and
# decompression bombs. Keep IMAGE_MAX_BYTES generous enough for a high-res
# phone JPEG (~10 MB on iPhone) but tight enough that an attacker can't fill
# the disk with one request. VIDEO_MAX_BYTES is sized for ~10 min of 720p
# H.264 at the bitrate react-native-compressor produces (2.5 Mbps ≈
# 18 MB/min).
IMAGE_MAX_BYTES = 30 * 1024 * 1024          # 30 MB per image
VIDEO_MAX_BYTES = 250 * 1024 * 1024         # 250 MB per video
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


