"""Unit tests for `services/media/validation.py` (audit B5).

Exercises the shared `validate_uploaded_media_file` and the underlying
`verify_uploaded_media` / `_sniff_audio_signature` / `_sniff_video_signature`
helpers directly with in-memory uploads. Decoupled from the views so it
doesn't need DB setup, request fixtures, or auth -- a regression in the
validator surfaces here long before it shows up in a chat-flow test.

The validator is the chokepoint for post / comment / DM / page-chat
uploads, so a single failure here means at least four endpoints just
lost their safety net. Keep these tests fast and deterministic.
"""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image

from api.services.media.validation import (
    AUDIO_MAX_BYTES,
    IMAGE_MAX_BYTES,
    VIDEO_MAX_BYTES,
    _sniff_audio_signature,
    _sniff_video_signature,
    validate_uploaded_media_file,
    verify_uploaded_media,
)


# ---------------------------------------------------------------------------
# Helpers: build well-formed in-memory uploads for each media kind.
# ---------------------------------------------------------------------------

def _png_bytes(width: int = 8, height: int = 8) -> bytes:
    """Generate a real PNG with Pillow so verify() actually walks it."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(128, 64, 32)).save(buf, format="PNG")
    return buf.getvalue()


def _mp4_header_bytes() -> bytes:
    """Minimal ISO-BMFF `ftyp` header. Enough to satisfy
    `_sniff_video_signature` -- we're not testing the demuxer, just the
    magic-byte sniff that runs before we ever pass the bytes to ffmpeg."""
    # 4 bytes size + 'ftyp' + 'mp42' brand + 4 bytes minor version + 'mp42isom'
    return b"\x00\x00\x00\x20ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 8


def _m4a_header_bytes() -> bytes:
    """ISO-BMFF with an M4A brand -- iOS voice notes look like this."""
    return b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00M4A mp42" + b"\x00" * 8


def _ogg_bytes() -> bytes:
    """OggS header bytes -- the audio path's most common web format."""
    return b"OggS" + b"\x00" * 28


def _wav_bytes() -> bytes:
    """Minimal RIFF/WAVE header."""
    return b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 20


def _mp3_id3_bytes() -> bytes:
    """MP3 with an ID3v2 tag (one of the two MP3 patterns we accept)."""
    return b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 20


def _upload(name: str, body: bytes, content_type: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, body, content_type=content_type)


# ---------------------------------------------------------------------------
# Happy-path: a real image, video header, and audio header are accepted.
# ---------------------------------------------------------------------------

class ValidatorHappyPathTests(SimpleTestCase):
    def test_real_png_is_accepted_as_image(self):
        f = _upload("ok.png", _png_bytes(), "image/png")
        self.assertEqual(
            validate_uploaded_media_file(f, allow_kinds=("image", "video")),
            "image",
        )
        # On success the cursor must be left at 0 -- downstream callers
        # immediately hand the file to FileField.save().
        self.assertEqual(f.tell(), 0)

    def test_mp4_header_is_accepted_as_video(self):
        f = _upload("clip.mp4", _mp4_header_bytes(), "video/mp4")
        self.assertEqual(
            validate_uploaded_media_file(f, allow_kinds=("image", "video")),
            "video",
        )
        self.assertEqual(f.tell(), 0)

    def test_m4a_header_is_accepted_as_audio(self):
        f = _upload("voice.m4a", _m4a_header_bytes(), "audio/m4a")
        self.assertEqual(
            validate_uploaded_media_file(
                f, allow_kinds=("image", "video", "audio")
            ),
            "audio",
        )
        self.assertEqual(f.tell(), 0)

    def test_ogg_is_accepted_as_audio(self):
        f = _upload("song.ogg", _ogg_bytes(), "audio/ogg")
        self.assertEqual(
            validate_uploaded_media_file(
                f, allow_kinds=("image", "video", "audio")
            ),
            "audio",
        )

    def test_wav_is_accepted_as_audio(self):
        f = _upload("sample.wav", _wav_bytes(), "audio/wav")
        self.assertEqual(
            validate_uploaded_media_file(
                f, allow_kinds=("image", "video", "audio")
            ),
            "audio",
        )

    def test_mp3_id3_is_accepted_as_audio(self):
        f = _upload("song.mp3", _mp3_id3_bytes(), "audio/mpeg")
        self.assertEqual(
            validate_uploaded_media_file(
                f, allow_kinds=("image", "video", "audio")
            ),
            "audio",
        )


# ---------------------------------------------------------------------------
# B5 core: the validator must REJECT what the old chat path used to accept.
# ---------------------------------------------------------------------------

class ValidatorRejectsB5RegressionsTests(SimpleTestCase):
    """These are the regressions B5 closes. Before the fix, every one of
    these would have been written to disk by the DM / page-chat send
    paths."""

    def test_rejects_executable_disguised_as_image(self):
        # ELF / PE / shell-script bytes inside a file with image/png
        # Content-Type. The magic-byte sniff catches it.
        f = _upload("evil.png", b"\x7fELF\x02\x01\x01\x00" + b"A" * 100, "image/png")
        with self.assertRaises(ValueError) as cm:
            validate_uploaded_media_file(
                f, allow_kinds=("image", "video", "audio"),
            )
        self.assertIn("valid image", str(cm.exception))

    def test_rejects_html_disguised_as_audio(self):
        f = _upload(
            "voice.m4a",
            b"<html><body>not really audio</body></html>",
            "audio/m4a",
        )
        with self.assertRaises(ValueError) as cm:
            validate_uploaded_media_file(
                f, allow_kinds=("image", "video", "audio"),
            )
        self.assertIn("audio", str(cm.exception).lower())

    def test_rejects_random_bytes_claimed_as_video(self):
        f = _upload(
            "clip.mp4",
            b"definitely not an mp4" + b"\x00" * 100,
            "video/mp4",
        )
        with self.assertRaises(ValueError) as cm:
            validate_uploaded_media_file(
                f, allow_kinds=("image", "video", "audio"),
            )
        self.assertIn("video", str(cm.exception).lower())

    def test_rejects_unsupported_top_level_content_type(self):
        # text/plain isn't in any allow_kinds; bounce before reading bytes.
        f = _upload("notes.txt", b"hello", "text/plain")
        with self.assertRaises(ValueError) as cm:
            validate_uploaded_media_file(
                f, allow_kinds=("image", "video", "audio"),
            )
        self.assertIn("Unsupported file type", str(cm.exception))

    def test_rejects_disallowed_kind_even_with_valid_bytes(self):
        # Audio bytes labelled audio/ogg -- but the caller only allows
        # image + video. Reject without sniffing.
        f = _upload("song.ogg", _ogg_bytes(), "audio/ogg")
        with self.assertRaises(ValueError) as cm:
            validate_uploaded_media_file(f, allow_kinds=("image", "video"))
        self.assertIn("Unsupported file type", str(cm.exception))

    def test_rejects_blank_content_type(self):
        f = _upload("mystery", _png_bytes(), "")
        with self.assertRaises(ValueError) as cm:
            validate_uploaded_media_file(
                f, allow_kinds=("image", "video", "audio"),
            )
        # The error should name "unknown" rather than echoing an empty
        # string -- empties read worse in the UI.
        self.assertIn("unknown", str(cm.exception))

    def test_rejects_content_type_vs_filename_mismatch(self):
        # `image.exe` posted as `image/png` -- the filename hint says
        # otherwise. Reject before sniffing.
        f = _upload("image.exe", _png_bytes(), "image/png")
        with self.assertRaises(ValueError) as cm:
            validate_uploaded_media_file(
                f, allow_kinds=("image", "video", "audio"),
            )
        self.assertIn("does not match", str(cm.exception))


# ---------------------------------------------------------------------------
# Size cap: the dollar-cost / DoS guard.
# ---------------------------------------------------------------------------

class ValidatorSizeCapTests(SimpleTestCase):
    def test_image_over_cap_rejected(self):
        f = _upload("huge.png", _png_bytes(), "image/png")
        # Override f.size directly because building a 30 MB BytesIO would
        # slow the test down; the validator reads f.size, not len(body).
        f.size = IMAGE_MAX_BYTES + 1
        with self.assertRaises(ValueError) as cm:
            validate_uploaded_media_file(f, allow_kinds=("image",))
        self.assertIn("Image exceeds", str(cm.exception))

    def test_video_over_cap_rejected(self):
        f = _upload("huge.mp4", _mp4_header_bytes(), "video/mp4")
        f.size = VIDEO_MAX_BYTES + 1
        with self.assertRaises(ValueError) as cm:
            validate_uploaded_media_file(f, allow_kinds=("video",))
        self.assertIn("Video exceeds", str(cm.exception))

    def test_audio_over_cap_rejected(self):
        f = _upload("huge.m4a", _m4a_header_bytes(), "audio/m4a")
        f.size = AUDIO_MAX_BYTES + 1
        with self.assertRaises(ValueError) as cm:
            validate_uploaded_media_file(f, allow_kinds=("audio",))
        self.assertIn("Audio exceeds", str(cm.exception))

    def test_per_callsite_override_applies(self):
        # Comment attachments could cap images at, say, 1 MB. The
        # `max_bytes_by_kind` override is how each endpoint tunes
        # without touching the global constants.
        f = _upload("ok.png", _png_bytes(), "image/png")
        f.size = 2 * 1024 * 1024  # 2 MB
        with self.assertRaises(ValueError) as cm:
            validate_uploaded_media_file(
                f,
                allow_kinds=("image",),
                max_bytes_by_kind={"image": 1 * 1024 * 1024},
            )
        self.assertIn("1 MB", str(cm.exception))


# ---------------------------------------------------------------------------
# Sniff helpers: cover the formats we explicitly enumerate.
# ---------------------------------------------------------------------------

class SniffHelpersTests(SimpleTestCase):
    def test_video_sniff_recognises_ftyp(self):
        self.assertTrue(_sniff_video_signature(_mp4_header_bytes()))

    def test_video_sniff_recognises_webm(self):
        self.assertTrue(_sniff_video_signature(b"\x1a\x45\xdf\xa3" + b"\x00" * 28))

    def test_video_sniff_rejects_garbage(self):
        self.assertFalse(_sniff_video_signature(b"NOT_VIDEO_BYTES" + b"\x00" * 16))

    def test_audio_sniff_recognises_ogg(self):
        self.assertTrue(_sniff_audio_signature(b"OggS" + b"\x00" * 16))

    def test_audio_sniff_recognises_wave(self):
        self.assertTrue(_sniff_audio_signature(b"RIFF\x00\x00\x00\x00WAVE"))

    def test_audio_sniff_recognises_flac(self):
        self.assertTrue(_sniff_audio_signature(b"fLaC" + b"\x00" * 16))

    def test_audio_sniff_recognises_mp3_id3(self):
        self.assertTrue(_sniff_audio_signature(b"ID3\x03" + b"\x00" * 16))

    def test_audio_sniff_recognises_mp3_frame_sync(self):
        # 0xFF E0..FF covers all MPEG version/layer combinations.
        self.assertTrue(_sniff_audio_signature(b"\xff\xfb" + b"\x00" * 16))

    def test_audio_sniff_rejects_garbage(self):
        self.assertFalse(_sniff_audio_signature(b"hello world" + b"\x00" * 16))


# ---------------------------------------------------------------------------
# Cursor reset: a non-negotiable contract for the caller pipeline.
# ---------------------------------------------------------------------------

class CursorResetTests(SimpleTestCase):
    """The validator's contract: on success, the file's read cursor is
    back at 0. Every caller hands the file straight to FileField.save()
    afterward; a cursor left mid-stream stores a truncated copy."""

    def test_cursor_reset_after_image_success(self):
        f = _upload("ok.png", _png_bytes(), "image/png")
        validate_uploaded_media_file(f, allow_kinds=("image",))
        self.assertEqual(f.tell(), 0)

    def test_cursor_reset_after_video_success(self):
        f = _upload("ok.mp4", _mp4_header_bytes(), "video/mp4")
        validate_uploaded_media_file(f, allow_kinds=("video",))
        self.assertEqual(f.tell(), 0)

    def test_cursor_reset_after_audio_success(self):
        f = _upload("ok.m4a", _m4a_header_bytes(), "audio/m4a")
        validate_uploaded_media_file(f, allow_kinds=("audio",))
        self.assertEqual(f.tell(), 0)


# ---------------------------------------------------------------------------
# Decompression-bomb behaviour: covered via the underlying verify path.
# ---------------------------------------------------------------------------

class DecompressionBombTests(SimpleTestCase):
    """Pillow exposes a `MAX_IMAGE_PIXELS` guard that converts oversize
    images into a `DecompressionBombError`. `verify_uploaded_media`
    catches it explicitly and re-raises as ValueError; this test confirms
    the wiring without actually building a 1.5-gigapixel PNG."""

    def test_bomb_error_maps_to_user_safe_message(self):
        original = Image.MAX_IMAGE_PIXELS
        try:
            # Tiny limit so an 8x8 PNG (64 px) is past 2x (DecompressionBombError).
            Image.MAX_IMAGE_PIXELS = 4
            f = _upload("bomb.png", _png_bytes(8, 8), "image/png")
            with self.assertRaises(ValueError) as cm:
                verify_uploaded_media(f, claimed_kind="image")
            self.assertIn("too large", str(cm.exception))
        finally:
            Image.MAX_IMAGE_PIXELS = original

    def test_bomb_warning_band_is_rejected(self):
        """An image OVER MAX_IMAGE_PIXELS but UNDER 2x it only triggers Pillow's
        DecompressionBombWarning, not the hard error. Pre-fix that slipped
        through verify(); we now promote the warning to an error so rejection
        lands at exactly MAX_IMAGE_PIXELS. 64-px image with the limit at 40:
        64 > 40 (warns) but 64 < 80 (no hard error)."""
        original = Image.MAX_IMAGE_PIXELS
        try:
            Image.MAX_IMAGE_PIXELS = 40
            f = _upload("warn.png", _png_bytes(8, 8), "image/png")
            with self.assertRaises(ValueError) as cm:
                verify_uploaded_media(f, claimed_kind="image")
            self.assertIn("too large", str(cm.exception))
        finally:
            Image.MAX_IMAGE_PIXELS = original

    def test_in_policy_image_passes(self):
        """Sanity floor: a normal small image under the ceiling is accepted and
        the cursor is reset for downstream consumers."""
        original = Image.MAX_IMAGE_PIXELS
        try:
            Image.MAX_IMAGE_PIXELS = 1_000_000  # 1 MP; an 8x8 is nowhere near
            f = _upload("ok.png", _png_bytes(8, 8), "image/png")
            verify_uploaded_media(f, claimed_kind="image")  # must not raise
            self.assertEqual(f.tell(), 0)
        finally:
            Image.MAX_IMAGE_PIXELS = original
