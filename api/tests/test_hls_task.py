"""Tests for the process_post_media Celery task (async HLS packaging, SY-1).

The task's *orchestration* is what's under test here — reading the stored MP4,
calling the builder/uploader, setting hls_master, idempotency, and graceful
failure. The actual ffmpeg encode is mocked (it's validated end-to-end
separately), so these run fast and without the binary.
"""
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from api import tasks
from api.models import Post, PostMedia


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ProcessPostMediaTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("hlsuser", password="x")
        self.post = Post.objects.create(user=self.user, description="clip")
        self.pm = PostMedia.objects.create(
            post=self.post,
            file=SimpleUploadedFile("clip.mp4", b"FAKE-MP4-BYTES", content_type="video/mp4"),
            order=0,
        )

    def test_happy_path_sets_hls_master(self):
        fake_bundle = ("master.m3u8", [("master.m3u8", b"#EXTM3U")])
        with mock.patch("api.services.media.build_hls_ladder", return_value=fake_bundle) as build, \
             mock.patch(
                 "api.services.media.store_hls_bundle",
                 return_value=("hls/deadbeef/master.m3u8", ["hls/deadbeef/master.m3u8"]),
             ) as store:
            tasks.process_post_media(self.pm.id)

        # Builder was handed the stored MP4 bytes; store was called with the bundle.
        build.assert_called_once_with(b"FAKE-MP4-BYTES")
        store.assert_called_once_with(fake_bundle)
        self.pm.refresh_from_db()
        self.assertEqual(self.pm.hls_master.name, "hls/deadbeef/master.m3u8")

    def test_idempotent_when_already_packaged(self):
        # Simulate a prior successful run.
        self.pm.hls_master.name = "hls/existing/master.m3u8"
        self.pm.save(update_fields=["hls_master"])

        with mock.patch("api.services.media.build_hls_ladder") as build, \
             mock.patch("api.services.media.store_hls_bundle") as store:
            tasks.process_post_media(self.pm.id)

        # Neither encode nor upload runs again; the existing master is untouched.
        build.assert_not_called()
        store.assert_not_called()
        self.pm.refresh_from_db()
        self.assertEqual(self.pm.hls_master.name, "hls/existing/master.m3u8")

    def test_encode_failure_leaves_hls_master_null(self):
        with mock.patch("api.services.media.build_hls_ladder", return_value=None), \
             mock.patch("api.services.media.store_hls_bundle") as store:
            tasks.process_post_media(self.pm.id)

        store.assert_not_called()
        self.pm.refresh_from_db()
        self.assertFalse(bool(self.pm.hls_master))

    def test_store_failure_leaves_hls_master_null(self):
        fake_bundle = ("master.m3u8", [("master.m3u8", b"#EXTM3U")])
        with mock.patch("api.services.media.build_hls_ladder", return_value=fake_bundle), \
             mock.patch("api.services.media.store_hls_bundle", side_effect=RuntimeError("s3 down")):
            tasks.process_post_media(self.pm.id)

        self.pm.refresh_from_db()
        self.assertFalse(bool(self.pm.hls_master))

    def test_missing_media_is_a_noop(self):
        # A deleted PostMedia between enqueue and run must not raise.
        tasks.process_post_media(999999)  # nonexistent id
