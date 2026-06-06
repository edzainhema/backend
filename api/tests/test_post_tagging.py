"""Tests for the per-media "tag people" feature on post create.

Covers the wire contract between the upload sheet (frontend) and create_post:
when the multipart POST carries `tagged_user_ids_{idx}` fields, the server
must persist matching PostMediaTag rows and fan out Notification rows + push
notifications to every tagged user (best-effort, skipping self / blocked).

These tests stub out the heavy media pipeline (Pillow / FFmpeg) and the push
sender so they run fast without a real broker or Firebase config -- the
behaviour we actually want to assert is the row writes + notification fan-out,
not the bytes that go to disk.
"""
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APITestCase

from api.models import BlockedUser, Notification, Post, PostMedia, PostMediaTag


def _register(client, username: str) -> str:
    """Register a user via the public auth endpoint and return their access
    token. Mirrors test_share_post._register so the helpers stay in sync."""
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "tag-pass-xyz",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


def _tiny_jpeg() -> bytes:
    """Return a few bytes of valid in-memory JPEG. Just enough for Pillow's
    verifier and the magic-byte sniff to accept it as a real image."""
    buf = BytesIO()
    Image.new("RGB", (4, 4), color=(200, 100, 50)).save(buf, format="JPEG")
    return buf.getvalue()


class PostTaggingTests(APITestCase):
    """End-to-end exercises of create_post's tagged_user_ids handling."""

    def setUp(self):
        # Clear the throttle bucket so the register call below stays under
        # the 5/min cap when this file runs after other auth-touching tests
        # in the same process. (Same one-line fix applied across the suite.)
        cache.clear()
        # Author + three potential tag targets. carol blocks alice in one test
        # below; the other two are clean baselines.
        self.alice_access = _register(self.client, "alice")
        self.alice = User.objects.get(username="alice")
        self.bob = User.objects.create_user("bob", password="x")
        self.carol = User.objects.create_user("carol", password="x")
        self.dave = User.objects.create_user("dave", password="x")

    def _auth_alice(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.alice_access}")

    def _upload(self, *, tagged_user_ids_0=None, push_mock=None):
        """Fire one multipart upload of a single image, optionally with a
        `tagged_user_ids_0` form field, and return the response. push_to_user
        is patched out by default so the test never needs a Celery broker /
        Firebase config; callers that care about the fan-out pass their own
        mock via `push_mock` to assert call args."""
        self._auth_alice()
        data = {
            "description": "hello",
            "files": SimpleUploadedFile(
                "img.jpg", _tiny_jpeg(), content_type="image/jpeg"
            ),
        }
        if tagged_user_ids_0 is not None:
            data["tagged_user_ids_0"] = tagged_user_ids_0
        # Patch the push helper at the import site in views.posts.create.
        # We want the Notification rows to land but the FCM call to be a
        # no-op (no broker / no Firebase needed in tests).
        target = "api.views.posts.create.push_to_user"
        with patch(target) as mock_push:
            if push_mock is not None:
                # Let the caller wire in their own mock so they can assert.
                mock_push.side_effect = push_mock
            resp = self.client.post("/posts/create/", data, format="multipart")
            self.last_push_mock = mock_push
        return resp

    # -------------------------------------------------------------------
    # Happy path
    # -------------------------------------------------------------------
    def test_creates_post_media_tag_rows_for_tagged_users(self):
        resp = self._upload(
            tagged_user_ids_0=f"[{self.bob.id}, {self.dave.id}]",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        post = Post.objects.get(id=resp.data["post"]["id"])
        media = PostMedia.objects.get(post=post)
        tagged_ids = set(
            PostMediaTag.objects.filter(media=media).values_list(
                "user_id", flat=True
            )
        )
        self.assertEqual(tagged_ids, {self.bob.id, self.dave.id})

    def test_creates_notification_per_tagged_user(self):
        self._upload(tagged_user_ids_0=f"[{self.bob.id}, {self.dave.id}]")
        bob_notifs = Notification.objects.filter(
            recipient=self.bob, notification_type="post_tag"
        )
        dave_notifs = Notification.objects.filter(
            recipient=self.dave, notification_type="post_tag"
        )
        self.assertEqual(bob_notifs.count(), 1)
        self.assertEqual(dave_notifs.count(), 1)
        # Sanity-check the actor + media wiring.
        n = bob_notifs.first()
        self.assertEqual(n.actor_id, self.alice.id)
        self.assertIsNotNone(n.media_id)

    def test_sends_push_to_each_tagged_user(self):
        self._upload(tagged_user_ids_0=f"[{self.bob.id}, {self.dave.id}]")
        # push_to_user is called once per tagged user (deduped across media).
        recipient_ids = {
            call.args[0].id for call in self.last_push_mock.call_args_list
        }
        self.assertEqual(recipient_ids, {self.bob.id, self.dave.id})
        # Body mentions the actor's username (the frontend renders this).
        for call in self.last_push_mock.call_args_list:
            self.assertIn("alice", call.kwargs["body"])

    # -------------------------------------------------------------------
    # Filtering rules
    # -------------------------------------------------------------------
    def test_silently_skips_self_tag(self):
        # Alice trying to tag herself: no PostMediaTag row, no notification.
        resp = self._upload(tagged_user_ids_0=f"[{self.alice.id}, {self.bob.id}]")
        self.assertEqual(resp.status_code, 201, resp.content)
        post = Post.objects.get(id=resp.data["post"]["id"])
        tagged_ids = set(
            PostMediaTag.objects.filter(media__post=post)
            .values_list("user_id", flat=True)
        )
        self.assertEqual(tagged_ids, {self.bob.id})
        # And the actor shouldn't have notified themselves.
        self.assertFalse(
            Notification.objects.filter(
                recipient=self.alice, notification_type="post_tag"
            ).exists()
        )

    def test_skips_blocked_user_in_either_direction(self):
        # Case 1: actor (alice) blocks carol.
        BlockedUser.objects.create(user=self.alice, blocked_user=self.carol)
        # Case 2: dave blocks actor (alice).
        BlockedUser.objects.create(user=self.dave, blocked_user=self.alice)

        resp = self._upload(
            tagged_user_ids_0=(
                f"[{self.bob.id}, {self.carol.id}, {self.dave.id}]"
            ),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        post = Post.objects.get(id=resp.data["post"]["id"])
        tagged_ids = set(
            PostMediaTag.objects.filter(media__post=post)
            .values_list("user_id", flat=True)
        )
        # Bob is the only one who isn't on either side of a block.
        self.assertEqual(tagged_ids, {self.bob.id})
        # And neither blocked user got a notification.
        self.assertFalse(
            Notification.objects.filter(
                recipient=self.carol, notification_type="post_tag"
            ).exists()
        )
        self.assertFalse(
            Notification.objects.filter(
                recipient=self.dave, notification_type="post_tag"
            ).exists()
        )

    def test_unknown_user_ids_are_silently_dropped(self):
        # Mix one real id with one non-existent one; the request still
        # succeeds and the real user is tagged. We do NOT 400 on unknown
        # ids -- a tag list is best-effort, not validated input.
        ghost_id = 99_999
        resp = self._upload(
            tagged_user_ids_0=f"[{self.bob.id}, {ghost_id}]",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        post = Post.objects.get(id=resp.data["post"]["id"])
        tagged_ids = set(
            PostMediaTag.objects.filter(media__post=post)
            .values_list("user_id", flat=True)
        )
        self.assertEqual(tagged_ids, {self.bob.id})

    def test_malformed_json_is_treated_as_no_tags(self):
        # Garbled JSON must not 400 the upload -- the post should still go
        # through with zero tagged users.
        resp = self._upload(tagged_user_ids_0="this is not json")
        self.assertEqual(resp.status_code, 201, resp.content)
        post = Post.objects.get(id=resp.data["post"]["id"])
        self.assertFalse(
            PostMediaTag.objects.filter(media__post=post).exists()
        )

    def test_omitted_field_creates_no_tags(self):
        # The common case: no tag picker interaction at all. Post still
        # uploads cleanly and produces no PostMediaTag / Notification rows.
        resp = self._upload(tagged_user_ids_0=None)
        self.assertEqual(resp.status_code, 201, resp.content)
        post = Post.objects.get(id=resp.data["post"]["id"])
        self.assertFalse(
            PostMediaTag.objects.filter(media__post=post).exists()
        )
        self.assertFalse(
            Notification.objects.filter(notification_type="post_tag").exists()
        )
