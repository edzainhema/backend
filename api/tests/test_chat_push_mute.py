"""Regression test for audit H2: the WebSocket chat push fan-out must respect
mutes, exactly like the REST path (M11).

`create_chat_message` (the service the WS consumer drives) builds the
per-recipient ``push_jobs`` list. Before this fix it filtered recipients only by
message-request acceptance (M7) and never by mute, so a recipient who had muted
the sender still got a push for every socket-sent message — and the socket is
the PRIMARY chat transport, so the mute-respecting REST path
(``_push_new_message`` -> ``push_to_user(actor=sender)``) was the one that
rarely ran. M11 was effectively bypassed for chat.

These tests drive the service directly and assert that a recipient who muted the
sender is dropped from ``push_jobs`` (while the message itself is still created
and broadcast), that the mute is directional, and that in a group only the
muting recipient is dropped.

Note on setup: a plain ``Conversation`` created here has no
``ConversationParticipantState`` rows, and ``get_accepted_recipient_ids``
grandfathers participants without a state row as *accepted* — so every recipient
is push-eligible by acceptance, which isolates the mute behaviour under test.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import Conversation, MutedUser
from api.services.chat import create_chat_message


def _recipient_ids(push_jobs):
    return {j["recipient_id"] for j in push_jobs}


class WsChatPushMuteTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(username="alice", password="x")
        self.bob = User.objects.create_user(username="bob", password="x")
        self.carol = User.objects.create_user(username="carol", password="x")

    def _convo(self, *members):
        convo = Conversation.objects.create()
        convo.participants.add(*members)
        return convo

    def test_unmuted_recipient_gets_push_job(self):
        """Baseline: with no mute, the accepted recipient is in push_jobs."""
        convo = self._convo(self.alice, self.bob)
        result = create_chat_message(convo.id, self.alice.id, "hi")
        self.assertIsNotNone(result)
        _msg, push_jobs = result
        self.assertIn(self.bob.id, _recipient_ids(push_jobs))

    def test_muted_recipient_dropped_from_push_jobs(self):
        """bob muted alice -> bob gets NO push for alice's DM, even though the
        message itself is still created and broadcast (only the push is
        suppressed, mirroring the REST path)."""
        convo = self._convo(self.alice, self.bob)
        MutedUser.objects.create(user=self.bob, muted_user=self.alice)
        result = create_chat_message(convo.id, self.alice.id, "hi")
        self.assertIsNotNone(
            result, "message must still be created; only the push is suppressed"
        )
        _msg, push_jobs = result
        self.assertNotIn(self.bob.id, _recipient_ids(push_jobs))

    def test_mute_is_directional(self):
        """alice muting bob must NOT suppress bob's push when ALICE sends —
        only the recipient muting the SENDER suppresses the push."""
        convo = self._convo(self.alice, self.bob)
        MutedUser.objects.create(user=self.alice, muted_user=self.bob)
        result = create_chat_message(convo.id, self.alice.id, "hi")
        self.assertIsNotNone(result)
        _msg, push_jobs = result
        self.assertIn(self.bob.id, _recipient_ids(push_jobs))

    def test_group_only_muting_recipient_dropped(self):
        """In a group, muting is per-recipient: bob (who muted alice) is dropped
        while carol (who didn't) still gets her push."""
        convo = self._convo(self.alice, self.bob, self.carol)
        MutedUser.objects.create(user=self.bob, muted_user=self.alice)
        result = create_chat_message(convo.id, self.alice.id, "hi all")
        self.assertIsNotNone(result)
        _msg, push_jobs = result
        ids = _recipient_ids(push_jobs)
        self.assertNotIn(self.bob.id, ids)
        self.assertIn(self.carol.id, ids)
