"""Tests for server-side image thumbnail generation (the small grid/feed
variant baked for image posts so the media grid decodes a tiny bitmap per cell
instead of the full-res file)."""
from io import BytesIO

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APITestCase
from unittest.mock import patch

from api.models import Post, PostMedia
from api.services.media import (
    make_image_thumbnail,
    IMAGE_THUMBNAIL_MAX_EDGE,
)


def _jpeg_bytes(w, h, color=(120, 40, 200)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), color=color).save(buf, format="JPEG")
    return buf.getvalue()


class MakeImageThumbnailUnitTests(APITestCase):
    """Direct exercises of the make_image_thumbnail helper."""

    def test_downscales_large_image_within_max_edge_preserving_aspect(self):
        src = SimpleUploadedFile("big.jpg", _jpeg_bytes(1600, 800), content_type="image/jpeg")
        thumb = make_image_thumbnail(src)
        self.assertIsNotNone(thumb)
        with Image.open(BytesIO(thumb.read())) as img:
            self.assertLessEqual(max(img.size), IMAGE_THUMBNAIL_MAX_EDGE)
            # 2:1 source aspect ratio is preserved within rounding.
            self.assertAlmostEqual(img.size[0] / img.size[1], 2.0, places=1)

    def test_resets_source_pointer_so_file_is_still_saveable(self):
        raw = _jpeg_bytes(1200, 1200)
        src = SimpleUploadedFile("a.jpg", raw, content_type="image/jpeg")
        make_image_thumbnail(src)
        # After thumbnailing, the original must read back its full bytes so the
        # SAME object can be handed to PostMedia.file (see create.py).
        self.assertEqual(src.read(), raw)

    def test_does_not_upscale_small_image(self):
        src = SimpleUploadedFile("tiny.jpg", _jpeg_bytes(50, 50), content_type="image/jpeg")
        thumb = make_image_thumbnail(src)
        self.assertIsNotNone(thumb)
        with Image.open(BytesIO(thumb.read())) as img:
            self.assertEqual(img.size, (50, 50))

    def test_returns_none_on_unreadable_input(self):
        bad = SimpleUploadedFile("bad.jpg", b"not an image", content_type="image/jpeg")
        self.assertIsNone(make_image_thumbnail(bad))

    def test_bakes_exif_orientation_into_pixels(self):
        # A landscape (800x400) image tagged Orientation=6 ("rotate 90° CW")
        # displays as portrait (400x800). The thumbnail must bake that rotation
        # into its pixels — otherwise the grid renders it sideways while the
        # full-res file (whose EXIF the client honours) looks upright.
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation tag
        buf = BytesIO()
        Image.new("RGB", (800, 400), color=(10, 200, 90)).save(
            buf, format="JPEG", exif=exif
        )
        src = SimpleUploadedFile("rotated.jpg", buf.getvalue(), content_type="image/jpeg")

        thumb = make_image_thumbnail(src)
        self.assertIsNotNone(thumb)
        with Image.open(BytesIO(thumb.read())) as img:
            # Post-transpose the image is portrait: height > width.
            self.assertGreater(img.size[1], img.size[0])


class ImagePostThumbnailIntegrationTests(APITestCase):
    """End-to-end: creating an image post populates PostMedia.thumbnail."""

    def setUp(self):
        cache.clear()
        resp = self.client.post("/auth/register/", {
            "username": "thumbuser",
            "password": "thumb-pass-xyz",
            "identifier_type": "email",
            "identifier": "thumbuser@example.com",
        }, format="json")
        assert resp.status_code == 200, resp.content
        self.access = resp.data["access"]

    def test_image_post_gets_a_thumbnail(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        data = {
            "description": "pic",
            "files": SimpleUploadedFile(
                "photo.jpg", _jpeg_bytes(1500, 1000), content_type="image/jpeg"
            ),
        }
        with patch("api.views.posts.create.push_to_user"):
            resp = self.client.post("/posts/create/", data, format="multipart")
        self.assertEqual(resp.status_code, 201, resp.content)
        post = Post.objects.get(id=resp.data["post"]["id"])
        media = PostMedia.objects.get(post=post)
        # Thumbnail column is populated...
        self.assertTrue(media.thumbnail and media.thumbnail.name)
        # ...the placeholder colour is a hex string...
        self.assertTrue(media.placeholder_color)
        self.assertTrue(media.placeholder_color.startswith("#"))
        self.assertEqual(len(media.placeholder_color), 7)
        # ...and the full-res file is intact (pointer reset didn't truncate it).
        self.assertGreater(media.file.size, 0)
        with Image.open(media.thumbnail) as t:
            self.assertLessEqual(max(t.size), IMAGE_THUMBNAIL_MAX_EDGE)


class BackfillImageThumbnailsCommandTests(APITestCase):
    """The management command fills thumbnails for legacy image rows only."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("legacy", password="x")
        self.post = Post.objects.create(user=self.user, description="old")

    def _media(self, name, data=b"", thumb=False):
        m = PostMedia.objects.create(
            post=self.post,
            file=SimpleUploadedFile(name, data or _jpeg_bytes(900, 600),
                                    content_type="image/jpeg"),
            order=0,
        )
        if thumb:
            m.thumbnail.save("t.jpg", SimpleUploadedFile("t.jpg", _jpeg_bytes(10, 10)), save=True)
        return m

    def test_backfills_only_image_rows_missing_a_thumbnail(self):
        from django.core.management import call_command

        img = self._media("legacy.jpg")
        vid = self._media("clip.mp4")
        already = self._media("hasthumb.jpg", thumb=True)
        already_name = already.thumbnail.name

        call_command("backfill_image_thumbnails")

        img.refresh_from_db()
        vid.refresh_from_db()
        already.refresh_from_db()
        self.assertTrue(img.thumbnail and img.thumbnail.name)
        self.assertFalse(bool(vid.thumbnail))
        self.assertEqual(already.thumbnail.name, already_name)
        # Placeholder colour is backfilled in the same pass.
        self.assertTrue(img.placeholder_color and img.placeholder_color.startswith("#"))

    def test_fix_dimensions_recomputes_exif_corrected_dims(self):
        from django.core.management import call_command

        # 800x400 landscape pixels + Orientation=6 displays as 400x800 portrait.
        # A legacy row stored the raw (wrong) landscape dims — the feed would
        # build a landscape box and crop the upright photo.
        exif = Image.Exif()
        exif[0x0112] = 6
        buf = BytesIO()
        Image.new("RGB", (800, 400), color=(20, 120, 200)).save(
            buf, format="JPEG", exif=exif
        )
        m = PostMedia.objects.create(
            post=self.post,
            file=SimpleUploadedFile("rotated.jpg", buf.getvalue(), content_type="image/jpeg"),
            order=0,
            width=800,
            height=400,
        )

        call_command("backfill_image_thumbnails", "--fix-dimensions")

        m.refresh_from_db()
        # Corrected to the EXIF-applied (displayed) orientation.
        self.assertEqual((m.width, m.height), (400, 800))

    def test_backfills_video_dimensions_from_thumbnail(self):
        from django.core.management import call_command

        # A legacy video row: no stored dimensions, but it has a thumbnail (the
        # first frame). The backfill reads the video's display size from that
        # thumbnail so the feed can size the tile instead of rendering square.
        m = PostMedia.objects.create(
            post=self.post,
            file=SimpleUploadedFile(
                "clip.mp4", b"not really a video", content_type="video/mp4"
            ),
            order=0,
        )
        m.thumbnail.save(
            "frame.jpg",
            SimpleUploadedFile("frame.jpg", _jpeg_bytes(720, 1280)),
            save=True,
        )
        self.assertIsNone(m.width)

        call_command("backfill_image_thumbnails")

        m.refresh_from_db()
        self.assertEqual((m.width, m.height), (720, 1280))

    def test_backfills_video_dimensions_from_thumbnail(self):
        from django.core.management import call_command

        # A legacy video row: no stored dimensions, but it has a thumbnail (the
        # first frame). The backfill reads the video's display size from that
        # thumbnail so the feed can size the tile instead of rendering square.
        m = PostMedia.objects.create(
            post=self.post,
            file=SimpleUploadedFile(
                "clip.mp4", b"not really a video", content_type="video/mp4"
            ),
            order=0,
        )
        m.thumbnail.save(
            "frame.jpg",
            SimpleUploadedFile("frame.jpg", _jpeg_bytes(720, 1280)),
            save=True,
        )
        self.assertIsNone(m.width)

        call_command("backfill_image_thumbnails")

        m.refresh_from_db()
        self.assertEqual((m.width, m.height), (720, 1280))
