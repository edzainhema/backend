"""Tests for the H5 per-message attachment cap.

`MAX_FILES_PER_MESSAGE` (10, defined in `services/chat.py`) limits how
many attachments one DM or page-chat send call accepts. Before this
cap, the collection loop in `_collect_typed_media` (DM) and
`send_page_chat_message` (page chat) was bounded only by Django's
`DATA_UPLOAD_MAX_NUMBER_FILES` (default 100), so a client could push
100 files per message and inflate the upload buffer + disk.

These tests cover both endpoints:
  * at-cap (10 attachments) is accepted
  * over-cap (11 attachments) is rejected with 400
  * single-attachment (legacy `media` field, not indexed) still works
"""
import io

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APITestCase

from api.models import Page, PageFollow, UserProfile


def _png_bytes() -> bytes:
    """Smallest valid PNG that passes `validate_uploaded_media_file`'s
    magic-byte sniff + Pillow probe. The H5 count check fires BEFORE
    per-file validation, so over-cap tests technically don't need real
    PNG bytes -- but at-cap tests do (they must reach storage), and
    using one helper across the file keeps test data uniform."""
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _png_upload(name: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, _png_bytes(), content_type="image/png")


def _register(client, username: str) -> str:
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "strong-pass-9999",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


class DmAttachmentCapTests(APITestCase):
    """H5 cap on /auth/send-message/ (DM send)."""

    def setUp(self):
        # B4 register throttle clear, same pattern as test_share_post.
        cache.clear()
        self.alice_access = _register(self.client, "alice")
        self.alice = User.objects.get(username="alice")
        self.bob_access = _register(self.client, "bob")
        self.bob = User.objects.get(username="bob")

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _send(self, n_attachments: int):
        """POST a DM with `n_attachments` indexed PNG files."""
        payload = {"target_user_id": str(self.bob.id)}
        for i in range(n_attachments):
            payload[f"media_{i}"] = _png_upload(f"a{i}.png")
        # multipart, not JSON -- file uploads can't go through JSON.
        return self.client.post(
            "/auth/send-message/", payload, format="multipart",
        )

    def test_at_cap_accepted(self):
        self._auth(self.alice_access)
        resp = self._send(10)
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_over_cap_rejected(self):
        self._auth(self.alice_access)
        resp = self._send(11)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Too many attachments", resp.data.get("error", ""))

    def test_one_attachment_accepted(self):
        """Sanity: the legacy single-`media` field path still works."""
        self._auth(self.alice_access)
        resp = self.client.post(
            "/auth/send-message/",
            {
                "target_user_id": str(self.bob.id),
                "media": _png_upload("solo.png"),
            },
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201, resp.content)


class PageChatAttachmentCapTests(APITestCase):
    """H5 cap on /pages/chat/send/ (page-chat send)."""

    def setUp(self):
        cache.clear()
        self.alice_access = _register(self.client, "alice")
        self.alice = User.objects.get(username="alice")
        # Page chat needs the page owner (or a follower) -- give alice
        # her own page so she's the owner and bypasses the membership
        # check.
        self.page = Page.objects.create(
            owner=self.alice, name="Alice's Page", chat_enabled=True,
        )

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _send(self, n_attachments: int):
        payload = {"page_id": str(self.page.id)}
        for i in range(n_attachments):
            payload[f"media_{i}"] = _png_upload(f"a{i}.png")
        return self.client.post(
            "/pages/chat/send/", payload, format="multipart",
        )

    def test_at_cap_accepted(self):
        self._auth(self.alice_access)
        resp = self._send(10)
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_over_cap_rejected(self):
        self._auth(self.alice_access)
        resp = self._send(11)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Too many attachments", resp.data.get("error", ""))

    def test_one_attachment_accepted(self):
        self._auth(self.alice_access)
        resp = self.client.post(
            "/pages/chat/send/",
            {
                "page_id": str(self.page.id),
                "media": _png_upload("solo.png"),
            },
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
