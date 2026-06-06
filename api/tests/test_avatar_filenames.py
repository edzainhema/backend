"""Tests for the M6 fix: avatar uploads use server-derived filenames.

Pre-fix, `update_profile_avatar` and `update_page_avatar` did:
    field = uploaded_file
    instance.save()
which let `FileField` persist whatever filename the multipart field
carried. The resulting public URL leaked the client's chosen string.
Post-fix, both views build the basename from the owning entity's id
(e.g. `profile_42_avatar`) and the EXTENSION from the file's actual
bytes (Pillow probe), then call `field.save(safe_name, file)`.

Direct unit tests of `safe_image_filename` cover the format-detection
contract; integration tests of both views verify the persisted name
on the model is what we expect (so the URL is what we expect).
"""
import io

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image
from rest_framework.test import APITestCase

from api.models import Page
from api.services.media.validation import safe_image_filename


# ---------- shared fixtures --------------------------------------------

def _png_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _gif_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("P", (width, height), color=1).save(buf, format="GIF")
    return buf.getvalue()


def _upload(name: str, body: bytes, content_type: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, body, content_type=content_type)


def _register(client, username: str) -> str:
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "strong-pass-9999",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


# ---------- unit tests on the helper -----------------------------------

class SafeImageFilenameTests(SimpleTestCase):
    """`safe_image_filename` must derive the extension from the FILE'S
    actual bytes, not from the client-supplied name. A misnamed PNG
    posted as `dog.jpg` must come back as `<prefix>.png`."""

    def test_png_returns_png_extension(self):
        f = _upload("anything.jpg", _png_bytes(), "image/png")
        self.assertEqual(
            safe_image_filename(f, "profile_42_avatar"),
            "profile_42_avatar.png",
        )

    def test_jpeg_returns_jpg_extension(self):
        f = _upload("whatever.png", _jpeg_bytes(), "image/jpeg")
        self.assertEqual(
            safe_image_filename(f, "page_7_avatar"),
            "page_7_avatar.jpg",
        )

    def test_gif_returns_gif_extension(self):
        f = _upload("anim.png", _gif_bytes(), "image/gif")
        self.assertEqual(
            safe_image_filename(f, "page_1_avatar"),
            "page_1_avatar.gif",
        )

    def test_garbage_falls_back_to_jpg(self):
        """Unreadable / non-image bytes shouldn't crash -- defensive
        fallback to `.jpg`. In production, validate_image_upload runs
        first and rejects garbage, so this path is mainly a guard."""
        f = _upload("evil.png", b"\x00\x01\x02 not an image", "image/png")
        self.assertEqual(
            safe_image_filename(f, "profile_99_avatar"),
            "profile_99_avatar.jpg",
        )

    def test_cursor_left_at_zero_on_success(self):
        """Caller chains the file straight into FileField.save(),
        which expects to read from position 0. If we leave the
        cursor mid-stream, FileField stores a truncated copy."""
        f = _upload("ok.png", _png_bytes(), "image/png")
        safe_image_filename(f, "profile_42_avatar")
        self.assertEqual(f.tell(), 0)

    def test_cursor_reset_even_on_failure(self):
        f = _upload("evil.png", b"garbage", "image/png")
        safe_image_filename(f, "profile_42_avatar")
        self.assertEqual(f.tell(), 0)


# ---------- integration tests on both views ----------------------------

class ProfileAvatarFilenameTests(APITestCase):
    """End-to-end: posting to /auth/profile/avatar/ should leave
    `userprofile.avatar.name` matching the server-derived pattern,
    not the client-supplied filename."""

    def setUp(self):
        cache.clear()
        self.access = _register(self.client, "alice")
        self.alice = User.objects.get(username="alice")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")

    def test_client_filename_is_not_persisted(self):
        # Client uploads a real PNG under a noisy / hostile filename.
        resp = self.client.post(
            "/auth/profile/avatar/",
            {"avatar": _upload(
                "../../etc/passwd.png", _png_bytes(), "image/png",
            )},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.alice.userprofile.refresh_from_db()
        name = self.alice.userprofile.avatar.name
        # The persisted name lives under avatars/ and starts with our
        # entity-derived prefix. The client's path-traversal-looking
        # filename does NOT appear anywhere in the stored name.
        self.assertTrue(
            name.startswith(f"avatars/profile_{self.alice.id}_avatar"),
            f"unexpected stored name: {name!r}",
        )
        self.assertNotIn("..", name)
        self.assertNotIn("etc", name)
        self.assertNotIn("passwd", name)

    def test_png_upload_gets_png_extension(self):
        resp = self.client.post(
            "/auth/profile/avatar/",
            {"avatar": _upload("avatar.png", _png_bytes(), "image/png")},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.alice.userprofile.refresh_from_db()
        # Storage may add a "_xxxxx" collision suffix between the
        # basename and the extension; the part we own ends in .png
        # because the bytes are PNG.
        self.assertTrue(self.alice.userprofile.avatar.name.endswith(".png"))

    def test_misnamed_jpeg_gets_jpg_extension(self):
        """Client posts JPEG bytes under a `.png` filename + image/png
        content-type. Extension on the stored name must reflect the
        REAL format (.jpg), not the client's lying filename."""
        resp = self.client.post(
            "/auth/profile/avatar/",
            {"avatar": _upload("liar.png", _jpeg_bytes(), "image/png")},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.alice.userprofile.refresh_from_db()
        self.assertTrue(self.alice.userprofile.avatar.name.endswith(".jpg"))


class PageAvatarFilenameTests(APITestCase):
    """End-to-end: posting to /pages/avatar/ should leave `page.avatar.name`
    matching the server-derived pattern."""

    def setUp(self):
        cache.clear()
        self.access = _register(self.client, "alice")
        self.alice = User.objects.get(username="alice")
        self.page = Page.objects.create(
            owner=self.alice, name="Alice's Page",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")

    def test_client_filename_is_not_persisted(self):
        resp = self.client.post(
            "/pages/avatar/",
            {
                "page_id": str(self.page.id),
                "avatar": _upload(
                    "weird name with spaces & symbols!.png",
                    _png_bytes(),
                    "image/png",
                ),
            },
            format="multipart",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.page.refresh_from_db()
        name = self.page.avatar.name
        self.assertTrue(
            name.startswith(f"page_avatars/page_{self.page.id}_avatar")
            or name.startswith(f"page_{self.page.id}_avatar"),
            f"unexpected stored name: {name!r}",
        )
        self.assertNotIn("weird", name)
        self.assertNotIn(" ", name)
        self.assertNotIn("&", name)

    def test_png_upload_gets_png_extension(self):
        resp = self.client.post(
            "/pages/avatar/",
            {
                "page_id": str(self.page.id),
                "avatar": _upload("logo.png", _png_bytes(), "image/png"),
            },
            format="multipart",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.page.refresh_from_db()
        self.assertTrue(self.page.avatar.name.endswith(".png"))

    def test_non_owner_cannot_upload(self):
        """Sanity: ownership guard fires BEFORE the new safe-name
        code path. A 403 means the file was never written, so the
        guard didn't accidentally regress when we added the M6 code."""
        cache.clear()
        # Different user trying to set someone else's page avatar.
        other_access = _register(self.client, "bob")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other_access}")
        resp = self.client.post(
            "/pages/avatar/",
            {
                "page_id": str(self.page.id),
                "avatar": _upload("hack.png", _png_bytes(), "image/png"),
            },
            format="multipart",
        )
        self.assertEqual(resp.status_code, 403)
