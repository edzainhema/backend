"""M7: message-requests / per-recipient acceptance state.

Covers:
  * Initial acceptance for 1:1 (follower vs non-follower).
  * Initial acceptance for groups (per-recipient bucketing).
  * Implicit accept on reply (REST + matches WS path via same service).
  * Push fan-out skips non-accepted recipients.
  * Inbox split (`/auth/conversations/` vs `/auth/conversations/requests/`).
  * Explicit accept + decline endpoints.
  * Grandfathering (legacy conversation w/ no state row stays in main inbox).
  * Request-creation throttle (per-sender, hourly + daily cap).
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import (
    Conversation,
    ConversationParticipantState,
    Follow,
)


def _register(client, username: str) -> str:
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "strong-pass-1234",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


class M7BaseMixin:
    """Shared setup: register alice + bob + carol + dave with no
    follow edges. Each test composes the follow graph it needs."""

    def setUp(self):
        # cache.clear() wipes the register throttle bucket AND the
        # M7 request-throttle counters so each test starts fresh.
        cache.clear()
        self.alice_access = _register(self.client, "alice")
        self.alice = User.objects.get(username="alice")
        self.bob_access = _register(self.client, "bob")
        self.bob = User.objects.get(username="bob")
        self.carol_access = _register(self.client, "carol")
        self.carol = User.objects.get(username="carol")
        self.dave_access = _register(self.client, "dave")
        self.dave = User.objects.get(username="dave")

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _follow(self, follower, target):
        Follow.objects.create(follower=follower, following=target)


class InitialAcceptanceTests(M7BaseMixin, APITestCase):
    """Conversation creation sets state.is_accepted based on whether the
    recipient follows the creator. The originator is always accepted."""

    def test_one_to_one_non_follower_lands_in_requests(self):
        # alice → bob. bob does NOT follow alice.
        self._auth(self.alice_access)
        resp = self.client.post(
            "/auth/start-conversation/",
            {"user_id": self.bob.id}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        convo_id = resp.data["conversation_id"]

        alice_state = ConversationParticipantState.objects.get(
            conversation_id=convo_id, user=self.alice,
        )
        bob_state = ConversationParticipantState.objects.get(
            conversation_id=convo_id, user=self.bob,
        )
        self.assertTrue(alice_state.is_accepted)
        self.assertFalse(bob_state.is_accepted)

    def test_one_to_one_follower_lands_direct(self):
        # bob follows alice => alice DMing bob should land in bob's
        # main inbox immediately (consent via follow).
        self._follow(self.bob, self.alice)

        self._auth(self.alice_access)
        resp = self.client.post(
            "/auth/start-conversation/",
            {"user_id": self.bob.id}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        bob_state = ConversationParticipantState.objects.get(
            conversation_id=resp.data["conversation_id"], user=self.bob,
        )
        self.assertTrue(bob_state.is_accepted)

    def test_group_per_recipient_bucketing(self):
        # bob follows alice, carol does NOT. dave is the third invitee.
        # carol + dave should both land in their requests inbox; bob
        # should land in his main inbox.
        self._follow(self.bob, self.alice)

        self._auth(self.alice_access)
        resp = self.client.post(
            "/auth/start-group-conversation/",
            {"user_ids": [self.bob.id, self.carol.id, self.dave.id],
             "name": "test grp"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        convo_id = resp.data["conversation_id"]

        alice_s = ConversationParticipantState.objects.get(
            conversation_id=convo_id, user=self.alice,
        )
        bob_s = ConversationParticipantState.objects.get(
            conversation_id=convo_id, user=self.bob,
        )
        carol_s = ConversationParticipantState.objects.get(
            conversation_id=convo_id, user=self.carol,
        )
        dave_s = ConversationParticipantState.objects.get(
            conversation_id=convo_id, user=self.dave,
        )
        self.assertTrue(alice_s.is_accepted)  # creator
        self.assertTrue(bob_s.is_accepted)    # follower
        self.assertFalse(carol_s.is_accepted)
        self.assertFalse(dave_s.is_accepted)


class ImplicitAcceptOnReplyTests(M7BaseMixin, APITestCase):
    """Sending a message in a conversation where the SENDER's state
    is_accepted=False flips it to True. Reading never accepts."""

    def setUp(self):
        super().setUp()
        # alice → bob, bob doesn't follow alice => bob's state starts False.
        self._auth(self.alice_access)
        resp = self.client.post(
            "/auth/start-conversation/",
            {"user_id": self.bob.id}, format="json",
        )
        self.convo_id = resp.data["conversation_id"]
        self.client.credentials()  # clear auth between assertions
        # Sanity: bob starts in requests.
        self.assertFalse(
            ConversationParticipantState.objects.get(
                conversation_id=self.convo_id, user=self.bob,
            ).is_accepted
        )

    def test_recipient_reply_flips_their_state(self):
        # bob replies => bob's state should flip True.
        self._auth(self.bob_access)
        resp = self.client.post(
            "/auth/send-message/",
            {"conversation_id": self.convo_id, "text": "hi back"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        bob_state = ConversationParticipantState.objects.get(
            conversation_id=self.convo_id, user=self.bob,
        )
        self.assertTrue(bob_state.is_accepted)
        self.assertIsNotNone(bob_state.accepted_at)

    def test_creator_replying_is_a_noop(self):
        # alice's state was True from creation; her further messages
        # shouldn't change the recipient's state.
        self._auth(self.alice_access)
        resp = self.client.post(
            "/auth/send-message/",
            {"conversation_id": self.convo_id, "text": "ping"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        bob_state = ConversationParticipantState.objects.get(
            conversation_id=self.convo_id, user=self.bob,
        )
        self.assertFalse(bob_state.is_accepted)


class PushGatingTests(M7BaseMixin, APITestCase):
    """`_push_new_message` skips recipients whose state is is_accepted=False
    so a message in a request thread is silent on their device."""

    @patch("api.views.messaging.messages.push_to_user")
    def test_no_push_to_non_accepted_recipient(self, mock_push):
        # alice → bob (non-follower) => bob's state False => no push.
        self._auth(self.alice_access)
        resp = self.client.post(
            "/auth/send-message/",
            {"target_user_id": self.bob.id, "text": "hello"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        mock_push.assert_not_called()

    @patch("api.views.messaging.messages.push_to_user")
    def test_push_fires_to_accepted_recipient(self, mock_push):
        # bob follows alice => bob's state True from the start => push fires.
        self._follow(self.bob, self.alice)
        self._auth(self.alice_access)
        resp = self.client.post(
            "/auth/send-message/",
            {"target_user_id": self.bob.id, "text": "hello"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(mock_push.called)
        # Recipient kwarg should be bob.
        call_args = mock_push.call_args
        self.assertEqual(call_args[0][0].id, self.bob.id)

    @patch("api.views.messaging.messages.push_to_user")
    def test_group_push_fires_only_to_accepted(self, mock_push):
        # bob follows alice, carol doesn't.
        # alice posts to {bob, carol} group => only bob gets pushed.
        self._follow(self.bob, self.alice)
        self._auth(self.alice_access)
        resp = self.client.post(
            "/auth/start-group-conversation/",
            {"user_ids": [self.bob.id, self.carol.id]}, format="json",
        )
        convo_id = resp.data["conversation_id"]

        mock_push.reset_mock()
        resp = self.client.post(
            "/auth/send-message/",
            {"conversation_id": convo_id, "text": "yo"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        recipient_ids = [c[0][0].id for c in mock_push.call_args_list]
        self.assertIn(self.bob.id, recipient_ids)
        self.assertNotIn(self.carol.id, recipient_ids)


class InboxSplitTests(M7BaseMixin, APITestCase):
    """`/auth/conversations/` returns accepted only; `/auth/conversations/
    requests/` returns the request bucket."""

    def setUp(self):
        super().setUp()
        # Pre-create three conversations from alice:
        #   * to bob (bob follows alice) -> direct on bob's side
        #   * to carol (carol does NOT follow alice) -> request on carol's
        #   * to dave (dave does NOT follow alice) -> request on dave's
        self._follow(self.bob, self.alice)
        self._auth(self.alice_access)
        self.bob_convo = self.client.post(
            "/auth/start-conversation/",
            {"user_id": self.bob.id}, format="json",
        ).data["conversation_id"]
        self.carol_convo = self.client.post(
            "/auth/start-conversation/",
            {"user_id": self.carol.id}, format="json",
        ).data["conversation_id"]
        self.dave_convo = self.client.post(
            "/auth/start-conversation/",
            {"user_id": self.dave.id}, format="json",
        ).data["conversation_id"]

    def test_alice_sees_all_three_in_main_inbox(self):
        # alice is the originator on all three => all accepted for her.
        self._auth(self.alice_access)
        resp = self.client.get("/auth/conversations/")
        self.assertEqual(resp.status_code, 200)
        ids = {r["conversation_id"] for r in resp.data["results"]}
        self.assertEqual(
            ids, {self.bob_convo, self.carol_convo, self.dave_convo},
        )
        # Requests list is empty for alice.
        resp = self.client.get("/auth/conversations/requests/")
        self.assertEqual(resp.data["results"], [])

    def test_carol_sees_only_request_in_requests_bucket(self):
        # carol has one request from alice; nothing in her main inbox.
        self._auth(self.carol_access)

        resp = self.client.get("/auth/conversations/")
        ids = {r["conversation_id"] for r in resp.data["results"]}
        self.assertNotIn(self.carol_convo, ids)

        resp = self.client.get("/auth/conversations/requests/")
        ids = {r["conversation_id"] for r in resp.data["results"]}
        self.assertEqual(ids, {self.carol_convo})

    def test_bob_sees_direct_convo_in_main_inbox(self):
        self._auth(self.bob_access)
        resp = self.client.get("/auth/conversations/")
        ids = {r["conversation_id"] for r in resp.data["results"]}
        self.assertEqual(ids, {self.bob_convo})

        resp = self.client.get("/auth/conversations/requests/")
        self.assertEqual(resp.data["results"], [])


class AcceptDeclineEndpointTests(M7BaseMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self._auth(self.alice_access)
        resp = self.client.post(
            "/auth/start-conversation/",
            {"user_id": self.bob.id}, format="json",
        )
        self.convo_id = resp.data["conversation_id"]

    def test_accept_flips_state(self):
        self._auth(self.bob_access)
        resp = self.client.post(
            "/auth/conversations/accept/",
            {"conversation_id": self.convo_id}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["status"], "accepted")
        self.assertTrue(
            ConversationParticipantState.objects.get(
                conversation_id=self.convo_id, user=self.bob,
            ).is_accepted
        )

    def test_accept_is_idempotent(self):
        self._auth(self.bob_access)
        self.client.post(
            "/auth/conversations/accept/",
            {"conversation_id": self.convo_id}, format="json",
        )
        resp = self.client.post(
            "/auth/conversations/accept/",
            {"conversation_id": self.convo_id}, format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "already_accepted")

    def test_non_participant_cannot_accept(self):
        self._auth(self.carol_access)
        resp = self.client.post(
            "/auth/conversations/accept/",
            {"conversation_id": self.convo_id}, format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_decline_hides_conversation(self):
        self._auth(self.bob_access)
        resp = self.client.post(
            "/auth/conversations/decline/",
            {"conversation_id": self.convo_id}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        # bob's requests inbox should no longer surface the convo.
        resp = self.client.get("/auth/conversations/requests/")
        ids = {r["conversation_id"] for r in resp.data["results"]}
        self.assertNotIn(self.convo_id, ids)


class GrandfatheringTests(M7BaseMixin, APITestCase):
    """A conversation with no state rows (legacy / pre-migration data
    the backfill somehow missed) must default to ACCEPTED so it stays
    in the user's main inbox -- never strand legitimate threads in
    requests as a side effect of shipping M7."""

    def test_conversation_without_state_rows_lands_in_main_inbox(self):
        # Create a conversation directly via ORM, skipping the view's
        # ensure_participant_states call -- this mimics legacy data.
        convo = Conversation.objects.create()
        convo.participants.add(self.alice, self.bob)
        # No state rows exist.
        self.assertFalse(
            ConversationParticipantState.objects.filter(
                conversation=convo,
            ).exists()
        )

        self._auth(self.alice_access)
        resp = self.client.get("/auth/conversations/")
        ids = {r["conversation_id"] for r in resp.data["results"]}
        self.assertIn(convo.id, ids)

        # And NOT in the requests bucket.
        resp = self.client.get("/auth/conversations/requests/")
        ids = {r["conversation_id"] for r in resp.data["results"]}
        self.assertNotIn(convo.id, ids)


class RequestThrottleTests(M7BaseMixin, APITestCase):
    """Per-sender hourly cap on opening new REQUEST conversations.
    Sending to a follower (direct, not a request) doesn't count."""

    def test_hourly_cap_blocks_after_threshold(self):
        # Create 5 fresh non-follower target users, exceed the hourly cap.
        targets = []
        for i in range(6):
            User.objects.create_user(f"target{i}", password="x")
            targets.append(User.objects.get(username=f"target{i}"))

        self._auth(self.alice_access)
        for i, target in enumerate(targets[:5]):
            resp = self.client.post(
                "/auth/start-conversation/",
                {"user_id": target.id}, format="json",
            )
            self.assertEqual(resp.status_code, 200, f"#{i}: {resp.content}")
        # 6th should trip the hourly cap (REQUEST_HOURLY_CAP=5).
        resp = self.client.post(
            "/auth/start-conversation/",
            {"user_id": targets[5].id}, format="json",
        )
        self.assertEqual(resp.status_code, 429, resp.content)
        self.assertIn("hour", resp.data["error"].lower())

    def test_starting_with_followers_does_not_consume_quota(self):
        # bob follows alice => starting with bob should not count.
        self._follow(self.bob, self.alice)
        self._auth(self.alice_access)
        for _ in range(10):  # well past the cap
            resp = self.client.post(
                "/auth/start-conversation/",
                {"user_id": self.bob.id}, format="json",
            )
            # All 200 because there's only one conversation (idempotent
            # re-opens of the same thread) AND bob is a follower so it's
            # not a request anyway.
            self.assertEqual(resp.status_code, 200, resp.content)

    def test_reopening_existing_conversation_does_not_consume_quota(self):
        # First call creates the conversation (counts).
        self._auth(self.alice_access)
        resp = self.client.post(
            "/auth/start-conversation/",
            {"user_id": self.bob.id}, format="json",
        )
        self.assertEqual(resp.status_code, 200)

        # Now create 4 more fresh request conversations -- should hit
        # cap on the 5th (1 + 4 = 5 total).
        for i in range(4):
            User.objects.create_user(f"x{i}", password="p")
            target = User.objects.get(username=f"x{i}")
            r = self.client.post(
                "/auth/start-conversation/",
                {"user_id": target.id}, format="json",
            )
            self.assertEqual(r.status_code, 200, f"new #{i}: {r.content}")

        # Re-opening alice→bob (already exists, not a NEW request) must
        # not be rejected by the throttle.
        resp = self.client.post(
            "/auth/start-conversation/",
            {"user_id": self.bob.id}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)


class SharePostThrottleTests(M7BaseMixin, APITestCase):
    """M7: the same per-sender request throttle that fires on
    start_conversation also gates share_post_to_users. Without this,
    a sender could spam request-state DMs by sharing a post to many
    strangers instead of using /start-conversation/."""

    def setUp(self):
        super().setUp()
        from api.models import Post
        self.post = Post.objects.create(
            user=self.alice, description="shareable",
        )

    def _share(self, user_ids):
        return self.client.post(
            "/auth/messages/share-post/",
            {"post_id": self.post.id, "user_ids": user_ids},
            format="json",
        )

    def test_share_to_followers_does_not_consume_quota(self):
        # bob + carol follow alice => sharing to them is direct, not a
        # request. Should never trip the throttle.
        self._follow(self.bob, self.alice)
        self._follow(self.carol, self.alice)
        self._auth(self.alice_access)
        # 10 share calls to followers -- well past cap.
        for i in range(10):
            resp = self._share([self.bob.id, self.carol.id])
            self.assertEqual(resp.status_code, 200, f"#{i}: {resp.content}")
            self.assertEqual(len(resp.data["failures"]), 0)

    def test_share_partial_throttle_when_cap_hit_mid_call(self):
        # Create 6 fresh non-follower targets.
        targets = []
        for i in range(6):
            User.objects.create_user(f"strangers{i}", password="x")
            targets.append(User.objects.get(username=f"strangers{i}"))
        self._auth(self.alice_access)

        # Single share to 6 non-followers. First 5 should succeed
        # (consuming the hourly quota); the 6th should fail with a
        # per-recipient throttle error.
        resp = self._share([t.id for t in targets])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["sent"]), 5)
        self.assertEqual(len(resp.data["failures"]), 1)
        self.assertEqual(resp.data["failures"][0]["user_id"], targets[5].id)
        self.assertIn("hour", resp.data["failures"][0]["error"].lower())

    def test_share_into_existing_thread_does_not_consume_quota(self):
        # Pre-create the convo (counts as the one initial request).
        self._auth(self.alice_access)
        first = self.client.post(
            "/auth/start-conversation/",
            {"user_id": self.bob.id}, format="json",
        )
        self.assertEqual(first.status_code, 200)
        # Burn the rest of the quota with start-conversation to 4 strangers.
        for i in range(4):
            User.objects.create_user(f"burn{i}", password="x")
            t = User.objects.get(username=f"burn{i}")
            r = self.client.post(
                "/auth/start-conversation/",
                {"user_id": t.id}, format="json",
            )
            self.assertEqual(r.status_code, 200, f"burn #{i}: {r.content}")

        # Quota now at cap (5). Sharing into the EXISTING alice↔bob
        # thread must still succeed -- it's not a new request.
        resp = self._share([self.bob.id])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["sent"]), 1)
        self.assertEqual(len(resp.data["failures"]), 0)

    def test_share_throttle_does_not_block_followers_in_mixed_recipients(self):
        # carol follows alice; the strangers don't.
        # Share to [carol, stranger1, stranger2, stranger3, stranger4,
        # stranger5, stranger6] -- carol always succeeds, strangers
        # consume quota up to cap (5), then fail.
        self._follow(self.carol, self.alice)
        strangers = []
        for i in range(6):
            User.objects.create_user(f"mixed{i}", password="x")
            strangers.append(User.objects.get(username=f"mixed{i}"))
        self._auth(self.alice_access)

        resp = self._share(
            [self.carol.id] + [s.id for s in strangers],
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        sent_ids = {e["user_id"] for e in resp.data["sent"]}
        # carol (follower) + first 5 strangers = 6 sent
        self.assertEqual(len(resp.data["sent"]), 6)
        self.assertIn(self.carol.id, sent_ids)
        # 6th stranger throttled
        self.assertEqual(len(resp.data["failures"]), 1)
        self.assertEqual(resp.data["failures"][0]["user_id"], strangers[5].id)
