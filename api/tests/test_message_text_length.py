"""Tests for the M10 fix: chat messages enforce a per-message text cap.

`MESSAGE_MAX_LEN` (10000, defined in `services/chat.py`) limits how
many characters a single chat message can carry. Without it, a client
could DM a multi-megabyte string and that string would land verbatim
in the FCM push body, the WebSocket frame, and the inbox preview --
each of which has its own (much tighter) practical ceiling, and any
one of which can OOM the recipient's phone trying to render.

Two code paths share the constant:
  * REST `/auth/send-message/` -> 400 with the limit named
  * `services/chat.create_chat_message` (used by the WS consumer)
    -> returns None (matching its other failure modes; the consumer's
    "result is None -> drop" path handles it).

`edit_chat_message`'s separate, tighter `MESSAGE_EDIT_MAX_LEN = 4000`
cap is unchanged and out of scope here -- edits are typo-fixes, not
novel rewrites.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import Conversation
from api.services.chat import (
    MESSAGE_EDIT_MAX_LEN,
    MESSAGE_MAX_LEN,
    create_chat_message,
)


def _register(client, username: str) -> str:
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "strong-pass-9999",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


class DmSendTextLengthTests(APITestCase):
    """REST /auth/send-message/ must reject text > MESSAGE_MAX_LEN
    with 400 BEFORE doing any per-file validation or DB work."""

    def setUp(self):
        # B4 throttle clear + the test class registers 2 users which
        # is under the 5/min cap, but cumulative across tests it isn't.
        cache.clear()
        self.alice_access = _register(self.client, "alice")
        self.alice = User.objects.get(username="alice")
        self.bob_access = _register(self.client, "bob")
        self.bob = User.objects.get(username="bob")

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _send(self, text: str):
        return self.client.post(
            "/auth/send-message/",
            {"target_user_id": self.bob.id, "text": text},
            format="json",
        )

    def test_at_cap_accepted(self):
        """A message at exactly the cap is allowed -- the boundary
        is inclusive on the low side. Confirms the cap isn't off-by-one."""
        self._auth(self.alice_access)
        resp = self._send("x" * MESSAGE_MAX_LEN)
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_over_cap_rejected(self):
        self._auth(self.alice_access)
        resp = self._send("x" * (MESSAGE_MAX_LEN + 1))
        self.assertEqual(resp.status_code, 400, resp.content)
        # Error message must include the numeric limit so a client UI
        # can render a useful "you typed N over the limit" toast.
        self.assertIn(str(MESSAGE_MAX_LEN), resp.data.get("error", ""))

    def test_normal_message_accepted(self):
        """Sanity: ordinary use stays in 201 territory."""
        self._auth(self.alice_access)
        resp = self._send("hello world")
        self.assertEqual(resp.status_code, 201, resp.content)


class CreateChatMessageServiceLengthTests(APITestCase):
    """Direct test of the service function the WS consumer drives."""

    def setUp(self):
        cache.clear()
        # Service path doesn't go through registration -- create_user
        # directly. The throttle isn't relevant here.
        self.alice = User.objects.create_user(username="alice", password="x")
        self.bob = User.objects.create_user(username="bob", password="x")
        self.convo = Conversation.objects.create()
        self.convo.participants.add(self.alice, self.bob)

    def test_at_cap_returns_message(self):
        result = create_chat_message(
            self.convo.id, self.alice.id, "x" * MESSAGE_MAX_LEN,
        )
        self.assertIsNotNone(result, "at-cap text must be accepted")
        msg_dict, _push_jobs = result
        self.assertEqual(len(msg_dict["text"]), MESSAGE_MAX_LEN)

    def test_over_cap_returns_none(self):
        # Service returns None to signal "don't broadcast" -- the
        # consumer's existing convention for the other failure modes
        # (deleted conversation, deleted sender, blocked-in-DM).
        # The WS client sees no echo; the frontend should cap the
        # input UI before this ever fires in practice.
        result = create_chat_message(
            self.convo.id, self.alice.id, "x" * (MESSAGE_MAX_LEN + 1),
        )
        self.assertIsNone(result)

    def test_normal_message_returns_message(self):
        result = create_chat_message(
            self.convo.id, self.alice.id, "hello",
        )
        self.assertIsNotNone(result)
        msg_dict, _push_jobs = result
        self.assertEqual(msg_dict["text"], "hello")


class EditCapIsUnchangedTests(APITestCase):
    """Regression guard: the edit cap (MESSAGE_EDIT_MAX_LEN = 4000) is
    intentionally tighter than the send cap (MESSAGE_MAX_LEN = 10000),
    and M10 must not have accidentally aligned them."""

    def test_edit_cap_is_strictly_less_than_send_cap(self):
        self.assertLess(MESSAGE_EDIT_MAX_LEN, MESSAGE_MAX_LEN)

    def test_constants_are_what_we_expect(self):
        # Sanity-pin both values so a future change has to update this
        # test deliberately, not silently.
        self.assertEqual(MESSAGE_EDIT_MAX_LEN, 4000)
        self.assertEqual(MESSAGE_MAX_LEN, 10000)
