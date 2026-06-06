"""Tests for the share-post-to-DMs feature."""
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import (
    BlockedUser,
    Conversation,
    Follow,
    Message,
    Post,
    UserCloseFriends,
)


def _register(client, username: str) -> str:
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "share-pass-xyz",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


# B4 introduced a 5/min cap on /auth/register/. DRF's throttle counters
# live in caches['default'] -- the same LocMemCache singleton across the
# whole Django test process -- so the cumulative register calls from
# every test in this file exhaust the bucket within seconds. Test
# transactions roll back DB writes but NOT cache state. Each test class
# that registers users now clears the throttle cache in setUp so it
# starts with a fresh bucket; same pattern RateLimitTests uses.


class ShareRecipientsTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.alice_access = _register(self.client, "alice")
        self.alice = User.objects.get(username="alice")
        self.bob_access = _register(self.client, "bob")
        self.bob = User.objects.get(username="bob")
        self.carol_access = _register(self.client, "carol")
        self.carol = User.objects.get(username="carol")

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def test_requires_auth(self):
        resp = self.client.get("/auth/messages/share-recipients/")
        self.assertEqual(resp.status_code, 401)

    def test_close_friends_rank_first(self):
        UserCloseFriends.objects.create(user=self.alice, friend_ids=[self.bob.id])
        Follow.objects.create(follower=self.alice, following=self.carol)
        self._auth(self.alice_access)
        resp = self.client.get("/auth/messages/share-recipients/")
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = [r["id"] for r in resp.data["results"]]
        self.assertIn(self.bob.id, ids)
        self.assertEqual(ids[0], self.bob.id)
        bob_row = next(r for r in resp.data["results"] if r["id"] == self.bob.id)
        self.assertTrue(bob_row["is_close_friend"])
        carol_row = next(
            (r for r in resp.data["results"] if r["id"] == self.carol.id), None
        )
        self.assertIsNotNone(carol_row)
        self.assertFalse(carol_row["is_close_friend"])

    def test_blocked_users_filtered(self):
        Follow.objects.create(follower=self.alice, following=self.bob)
        BlockedUser.objects.create(user=self.alice, blocked_user=self.bob)
        self._auth(self.alice_access)
        resp = self.client.get("/auth/messages/share-recipients/")
        self.assertEqual(resp.status_code, 200)
        ids = [r["id"] for r in resp.data["results"]]
        self.assertNotIn(self.bob.id, ids)

    def test_self_excluded(self):
        self._auth(self.alice_access)
        resp = self.client.get("/auth/messages/share-recipients/")
        self.assertEqual(resp.status_code, 200)
        ids = [r["id"] for r in resp.data["results"]]
        self.assertNotIn(self.alice.id, ids)


class SharePostTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.alice_access = _register(self.client, "alice")
        self.alice = User.objects.get(username="alice")
        self.bob_access = _register(self.client, "bob")
        self.bob = User.objects.get(username="bob")
        self.carol_access = _register(self.client, "carol")
        self.carol = User.objects.get(username="carol")
        self.post = Post.objects.create(user=self.alice, description="hello world")

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def test_requires_auth(self):
        resp = self.client.post("/auth/messages/share-post/", {}, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_rejects_empty_user_list(self):
        self._auth(self.alice_access)
        resp = self.client.post("/auth/messages/share-post/", {
            "post_id": self.post.id,
            "user_ids": [],
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_share_creates_messages_per_recipient(self):
        self._auth(self.alice_access)
        resp = self.client.post("/auth/messages/share-post/", {
            "post_id": self.post.id,
            "user_ids": [self.bob.id, self.carol.id],
            "text": "check this out",
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["sent"]), 2)
        self.assertEqual(resp.data["failures"], [])
        sent_user_ids = {e["user_id"] for e in resp.data["sent"]}
        self.assertEqual(sent_user_ids, {self.bob.id, self.carol.id})
        for entry in resp.data["sent"]:
            convo = Conversation.objects.get(id=entry["conversation_id"])
            self.assertEqual(convo.participants.count(), 2)
            msg = Message.objects.get(id=entry["message_id"])
            self.assertEqual(msg.shared_post_id, self.post.id)
            self.assertEqual(msg.text, "check this out")

    def test_share_reuses_existing_dm(self):
        convo = Conversation.objects.create()
        convo.participants.add(self.alice, self.bob)
        self._auth(self.alice_access)
        resp = self.client.post("/auth/messages/share-post/", {
            "post_id": self.post.id,
            "user_ids": [self.bob.id],
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["sent"][0]["conversation_id"], convo.id)

    def test_share_blocked_recipient_reported_as_failure(self):
        BlockedUser.objects.create(user=self.alice, blocked_user=self.bob)
        self._auth(self.alice_access)
        resp = self.client.post("/auth/messages/share-post/", {
            "post_id": self.post.id,
            "user_ids": [self.bob.id, self.carol.id],
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["sent"]), 1)
        self.assertEqual(resp.data["sent"][0]["user_id"], self.carol.id)
        self.assertEqual(len(resp.data["failures"]), 1)
        self.assertEqual(resp.data["failures"][0]["user_id"], self.bob.id)

    def test_shared_post_appears_in_get_messages(self):
        self._auth(self.alice_access)
        resp = self.client.post("/auth/messages/share-post/", {
            "post_id": self.post.id,
            "user_ids": [self.bob.id],
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        convo_id = resp.data["sent"][0]["conversation_id"]
        self._auth(self.bob_access)
        resp = self.client.get(f"/auth/messages/?conversation_id={convo_id}")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["results"]), 1)
        msg = resp.data["results"][0]
        self.assertIsNotNone(msg["shared_post"])
        self.assertEqual(msg["shared_post"]["id"], self.post.id)
        self.assertEqual(msg["shared_post"]["author"]["id"], self.alice.id)
        self.assertEqual(msg["shared_post"]["author"]["username"], "alice")

    def test_inbox_preview_for_sender_says_you_sent(self):
        self._auth(self.alice_access)
        resp = self.client.post("/auth/messages/share-post/", {
            "post_id": self.post.id,
            "user_ids": [self.bob.id],
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self.client.get("/auth/conversations/")
        self.assertEqual(resp.status_code, 200, resp.content)
        results = resp.data["results"]
        self.assertTrue(results)
        row = results[0]
        self.assertEqual(row["last_message"], "You sent a post by alice")
        self.assertNotEqual(row["last_message"], "")

    def test_inbox_preview_for_recipient_names_sender(self):
        # M7: bob must follow alice for the shared post to land in his
        # MAIN inbox. Without the follow, the conversation is in bob's
        # requests inbox and the recipient-side preview formatting is
        # tested via /auth/conversations/requests/ instead. This test
        # is about the formatting string, not about M7 -- add the
        # follow so the convo lands in the bucket the test queries.
        from api.models import Follow
        Follow.objects.create(follower=self.bob, following=self.alice)

        self._auth(self.alice_access)
        resp = self.client.post("/auth/messages/share-post/", {
            "post_id": self.post.id,
            "user_ids": [self.bob.id],
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self._auth(self.bob_access)
        resp = self.client.get("/auth/conversations/")
        self.assertEqual(resp.status_code, 200, resp.content)
        results = resp.data["results"]
        self.assertTrue(results)
        row = results[0]
        self.assertEqual(row["last_message"], "alice sent a post by alice")

    def test_unknown_post_returns_404(self):
        self._auth(self.alice_access)
        resp = self.client.post("/auth/messages/share-post/", {
            "post_id": 99999,
            "user_ids": [self.bob.id],
        }, format="json")
        self.assertEqual(resp.status_code, 404)


class SharePostVisibilityTests(APITestCase):
    """Audit H1: share_post_to_users must refuse to forward a post the
    sender can't see -- otherwise a viewer who guesses a post id from a
    private page / private-account profile can leak the preview card
    (author, thumbnail, description) into DMs.

    Mirrors the gate `toggle_post_like` / `get_comments` apply. The
    refusal status MUST match the genuinely-missing-post case (404 with
    "Post not found.") so the endpoint can't be used as a probe for
    hidden content.
    """

    def setUp(self):
        cache.clear()
        from api.models import Page, PageFollow, UserProfile
        self.Page = Page
        self.PageFollow = PageFollow
        self.UserProfile = UserProfile

        # alice = sharer attempting to share. carol = the person alice
        # tries to share TO (an unrelated third party so we never test
        # self-shares by accident).
        self.alice_access = _register(self.client, "alice")
        self.alice = User.objects.get(username="alice")
        self.carol_access = _register(self.client, "carol")
        self.carol = User.objects.get(username="carol")

        # owner = author of the hidden content. Their profile is set up
        # in each test depending on what's being hidden (private profile
        # vs. private page).
        self.owner_access = _register(self.client, "ownerx")
        self.owner = User.objects.get(username="ownerx")

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _share(self, post_id: int):
        return self.client.post(
            "/auth/messages/share-post/",
            {"post_id": post_id, "user_ids": [self.carol.id]},
            format="json",
        )

    # ---------- private-profile (no page) author -------------------------

    def test_private_account_post_cannot_be_shared_by_non_follower(self):
        # Owner flips their account private; alice doesn't follow them.
        prof = self.owner.userprofile
        prof.is_private = True
        prof.save(update_fields=["is_private"])
        hidden = Post.objects.create(user=self.owner, description="secret")

        self._auth(self.alice_access)
        resp = self._share(hidden.id)
        self.assertEqual(resp.status_code, 404, resp.content)
        # Same error string as the genuinely-missing case below, so the
        # endpoint can't double as an existence oracle.
        self.assertEqual(resp.data.get("error"), "Post not found.")

    def test_private_account_post_shareable_by_follower(self):
        from api.models import Follow
        prof = self.owner.userprofile
        prof.is_private = True
        prof.save(update_fields=["is_private"])
        Follow.objects.create(follower=self.alice, following=self.owner)
        visible = Post.objects.create(user=self.owner, description="hi friends")

        self._auth(self.alice_access)
        resp = self._share(visible.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["sent"]), 1)

    # ---------- private page post ----------------------------------------

    def test_private_page_post_cannot_be_shared_by_non_follower(self):
        page = self.Page.objects.create(
            owner=self.owner, name="Secret Club", is_private=True,
        )
        hidden = Post.objects.create(
            user=self.owner, page=page, description="members only",
        )

        self._auth(self.alice_access)
        resp = self._share(hidden.id)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(resp.data.get("error"), "Post not found.")

    def test_private_page_post_shareable_by_page_follower(self):
        page = self.Page.objects.create(
            owner=self.owner, name="Secret Club", is_private=True,
        )
        self.PageFollow.objects.create(user=self.alice, page=page)
        visible = Post.objects.create(
            user=self.owner, page=page, description="members only",
        )

        self._auth(self.alice_access)
        resp = self._share(visible.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["sent"]), 1)

    # ---------- super-private page post (existence-hiding) ---------------

    def test_super_private_page_post_cannot_be_shared_by_non_follower(self):
        page = self.Page.objects.create(
            owner=self.owner, name="Vault", is_super_private=True,
        )
        hidden = Post.objects.create(
            user=self.owner, page=page, description="vault content",
        )

        self._auth(self.alice_access)
        resp = self._share(hidden.id)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(resp.data.get("error"), "Post not found.")

    # ---------- own posts always shareable -------------------------------

    def test_own_private_post_always_shareable(self):
        # Even if alice's account is private and she has zero followers,
        # she can still share her own posts.
        prof = self.alice.userprofile
        prof.is_private = True
        prof.save(update_fields=["is_private"])
        own = Post.objects.create(user=self.alice, description="me me me")

        self._auth(self.alice_access)
        resp = self._share(own.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["sent"]), 1)

    # ---------- public post smoke-test (regression guard) ----------------

    def test_public_post_still_shareable(self):
        # Sanity: the new gate must NOT have broken the normal case.
        public = Post.objects.create(user=self.owner, description="open")

        self._auth(self.alice_access)
        resp = self._share(public.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["sent"]), 1)

    # ---------- is_public_override escape hatch -------------------------

    def test_private_page_post_with_public_override_shareable_by_author_follower(self):
        # The author marked a single post on a private page as
        # is_public_override=True. Followers of the AUTHOR (not the page)
        # can see it -- so they can share it.
        from api.models import Follow
        page = self.Page.objects.create(
            owner=self.owner, name="Secret Club", is_private=True,
        )
        Follow.objects.create(follower=self.alice, following=self.owner)
        leaked = Post.objects.create(
            user=self.owner,
            page=page,
            description="this one is fine to share",
            is_public_override=True,
        )

        self._auth(self.alice_access)
        resp = self._share(leaked.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["sent"]), 1)
