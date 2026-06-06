"""Tests for the M11 fix: push notifications respect recipient mutes.

`push_to_user` grew optional `actor` / `page` kwargs. When passed, it
checks `MutedUser` / `MutedPage` and silently skips the push if the
recipient has muted the actor or the page. Callers that don't pass the
kwargs keep their legacy behaviour (no mute check), so this is
backward-compatible.

The audit named two callers:
  * `_push_new_message` (DM send) -- now passes actor=sender
  * `_notify_post_tags` (post tagging) -- now passes actor=author, page=post.page

The other push callers (likes, comments, follows, page invites, ...)
weren't named by the audit and aren't migrated here. If you want them
to respect mutes too, the migration is just `actor=...` / `page=...`
at each call site -- the gate is centralized in `push_to_user` now.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APITestCase

from api.models import (
    Conversation, MutedPage, MutedUser, Page, Post, PostMedia, PostMediaTag,
)
from api.services.push import push_to_user


def _register(client, username: str) -> str:
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "strong-pass-9999",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


class PushToUserMuteGateUnitTests(TestCase):
    """Direct tests on `push_to_user`'s mute-gating behaviour. Mocks
    `dispatch_push.delay` so we can assert whether the Celery enqueue
    happened or got short-circuited."""

    def setUp(self):
        self.recipient = User.objects.create_user(
            username="recipient", password="x",
        )
        self.actor = User.objects.create_user(
            username="actor", password="x",
        )
        self.page = Page.objects.create(
            owner=self.actor, name="Actor's Page",
        )

    def test_no_kwargs_enqueues_push(self):
        """Backward compat: callers that don't pass actor/page get
        the legacy behaviour -- no mute check, push always enqueued."""
        with patch("api.tasks.dispatch_push.delay") as mock_delay:
            push_to_user(self.recipient, "t", "b")
            mock_delay.assert_called_once()

    def test_actor_kwarg_unmuted_enqueues_push(self):
        with patch("api.tasks.dispatch_push.delay") as mock_delay:
            push_to_user(self.recipient, "t", "b", actor=self.actor)
            mock_delay.assert_called_once()

    def test_actor_kwarg_muted_skips_push(self):
        MutedUser.objects.create(
            user=self.recipient, muted_user=self.actor,
        )
        with patch("api.tasks.dispatch_push.delay") as mock_delay:
            push_to_user(self.recipient, "t", "b", actor=self.actor)
            mock_delay.assert_not_called()

    def test_page_kwarg_muted_skips_push(self):
        MutedPage.objects.create(user=self.recipient, page=self.page)
        with patch("api.tasks.dispatch_push.delay") as mock_delay:
            push_to_user(self.recipient, "t", "b", page=self.page)
            mock_delay.assert_not_called()

    def test_actor_xor_page_muted_skips_push(self):
        """Either condition alone is enough -- it's an OR, not an AND."""
        MutedUser.objects.create(
            user=self.recipient, muted_user=self.actor,
        )
        # Page is NOT muted, but actor is. Push should still be skipped.
        with patch("api.tasks.dispatch_push.delay") as mock_delay:
            push_to_user(
                self.recipient, "t", "b",
                actor=self.actor, page=self.page,
            )
            mock_delay.assert_not_called()

    def test_other_users_mute_does_not_affect_recipient(self):
        """Mute scope is per-user: someone ELSE muting the actor must
        not suppress pushes to this recipient."""
        other = User.objects.create_user(username="other", password="x")
        MutedUser.objects.create(user=other, muted_user=self.actor)
        with patch("api.tasks.dispatch_push.delay") as mock_delay:
            push_to_user(self.recipient, "t", "b", actor=self.actor)
            mock_delay.assert_called_once()


class DmPushMuteIntegrationTests(APITestCase):
    """End-to-end: when alice has muted bob and bob DMs alice, the FCM
    enqueue is skipped. The DM itself still goes through; only the
    push buzz is suppressed (Notification rows are out of scope here
    -- DMs don't create Notification rows, they live in the inbox)."""

    def setUp(self):
        cache.clear()
        self.alice_access = _register(self.client, "alice")
        self.alice = User.objects.get(username="alice")
        self.bob_access = _register(self.client, "bob")
        self.bob = User.objects.get(username="bob")

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def test_dm_push_skipped_when_recipient_muted_sender(self):
        # alice mutes bob. M7: alice must ALSO follow bob so the DM lands
        # in alice's main inbox (otherwise the push would be suppressed
        # for being a request, not for being muted -- two different gates).
        from api.models import Follow
        Follow.objects.create(follower=self.alice, following=self.bob)

        MutedUser.objects.create(user=self.alice, muted_user=self.bob)
        self._auth(self.bob_access)

        with patch("api.tasks.dispatch_push.delay") as mock_delay:
            resp = self.client.post(
                "/auth/send-message/",
                {"target_user_id": self.alice.id, "text": "hi"},
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        # Push was NOT enqueued for the muted recipient.
        mock_delay.assert_not_called()

    def test_dm_push_enqueued_when_recipient_did_not_mute_sender(self):
        """Regression sanity: a normal (unmuted) DM still pushes.
        M7: alice must follow bob for this conversation to be direct
        (not a request). Without the follow, M7 would suppress the
        push for being a request -- and this test is about mute, not
        about M7's request gating."""
        from api.models import Follow
        Follow.objects.create(follower=self.alice, following=self.bob)

        self._auth(self.bob_access)
        with patch("api.tasks.dispatch_push.delay") as mock_delay:
            resp = self.client.post(
                "/auth/send-message/",
                {"target_user_id": self.alice.id, "text": "hi"},
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        mock_delay.assert_called_once()


class PostTagPushMuteIntegrationTests(APITestCase):
    """End-to-end: when a tagged user has muted the poster (or the
    page), the post-tag push is skipped. Uses direct model setup
    instead of going through /posts/create/ -- create_post involves
    file validation + ffmpeg + transactional storage writes that
    aren't relevant to the M11 check."""

    def setUp(self):
        cache.clear()
        # poster: the one who creates the post + does the tagging.
        # tagged: the one who gets tagged (and might mute poster).
        self.poster = User.objects.create_user(username="poster", password="x")
        self.tagged = User.objects.create_user(username="tagged", password="x")
        self.page = Page.objects.create(
            owner=self.poster, name="Poster's Page",
        )
        self.post = Post.objects.create(
            user=self.poster, page=self.page, description="check it",
        )
        self.media = PostMedia.objects.create(
            post=self.post, file="dummy.jpg", order=0,
        )
        # Create the tag row so _notify_post_tags has something to read.
        PostMediaTag.objects.create(media=self.media, user=self.tagged)

    def _trigger_notify(self):
        """Drive `_notify_post_tags` with a fake request object that
        has `.user = self.poster`. The helper only reads `request.user`,
        so a stub is enough."""
        from api.views.posts.create import _notify_post_tags
        class _Stub:
            user = self.poster
        _notify_post_tags(_Stub(), self.post)

    def test_post_tag_push_skipped_when_tagged_muted_poster(self):
        MutedUser.objects.create(user=self.tagged, muted_user=self.poster)
        with patch("api.tasks.dispatch_push.delay") as mock_delay:
            self._trigger_notify()
        mock_delay.assert_not_called()

    def test_post_tag_push_skipped_when_tagged_muted_page(self):
        MutedPage.objects.create(user=self.tagged, page=self.page)
        with patch("api.tasks.dispatch_push.delay") as mock_delay:
            self._trigger_notify()
        mock_delay.assert_not_called()

    def test_post_tag_push_fires_normally_without_mute(self):
        with patch("api.tasks.dispatch_push.delay") as mock_delay:
            self._trigger_notify()
        mock_delay.assert_called_once()
