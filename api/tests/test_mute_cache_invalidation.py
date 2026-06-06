"""Regression test for audit M1: unmute must invalidate the viewer's feed cache.

The mute branch of `toggle_mute_user` cleared `feed_ctx:{uid}` +
`suggested_feed_scores:{uid}` so the muted author dropped out of the next feed
load. The unmute branch cleared nothing, so a just-unmuted author stayed
filtered out for up to the feed_ctx TTL (~90s) — the unmute looked broken. This
asserts BOTH branches now invalidate, symmetric with toggle_block_user.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import MutedUser


class ToggleMuteCacheInvalidationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(username="alice", password="x")
        self.bob = User.objects.create_user(username="bob", password="x")
        self.client.force_authenticate(self.alice)
        self.ctx_key = f"feed_ctx:{self.alice.id}"
        self.scores_key = f"suggested_feed_scores:{self.alice.id}"

    def _prime_cache(self):
        # Stand in for warm per-viewer feed caches built before the toggle.
        cache.set(self.ctx_key, {"muted_user_ids": set()}, 90)
        cache.set(self.scores_key, [(1, 1.0)], 300)

    def test_mute_invalidates_feed_cache(self):
        self._prime_cache()
        resp = self.client.post("/auth/mute/", {"user_id": self.bob.id}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["status"], "muted")
        self.assertIsNone(cache.get(self.ctx_key))
        self.assertIsNone(cache.get(self.scores_key))

    def test_unmute_invalidates_feed_cache(self):
        # Start already-muted, then unmute with warm caches.
        MutedUser.objects.create(user=self.alice, muted_user=self.bob)
        self._prime_cache()
        resp = self.client.post("/auth/mute/", {"user_id": self.bob.id}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["status"], "unmuted")
        # The M1 fix: these must be cleared on the unmute branch too.
        self.assertIsNone(cache.get(self.ctx_key))
        self.assertIsNone(cache.get(self.scores_key))

    def test_unmute_actually_removes_the_row(self):
        MutedUser.objects.create(user=self.alice, muted_user=self.bob)
        self.client.post("/auth/mute/", {"user_id": self.bob.id}, format="json")
        self.assertFalse(
            MutedUser.objects.filter(user=self.alice, muted_user=self.bob).exists()
        )

    def test_mute_does_not_touch_notification_cache(self):
        """Mute must NOT invalidate the notification unread-count cache —
        notification listings filter on blocks, not mutes. Guards against a
        future 'symmetry' change that wrongly clears it."""
        notif_key = f"unread_notif_count:{self.alice.id}"
        cache.set(notif_key, 7, 30)
        self.client.post("/auth/mute/", {"user_id": self.bob.id}, format="json")
        self.assertEqual(cache.get(notif_key), 7)

    def test_cannot_mute_self(self):
        """L4: self-mute is rejected with 400 and creates no row."""
        from api.models import MutedUser
        resp = self.client.post(
            "/auth/mute/", {"user_id": self.alice.id}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(
            MutedUser.objects.filter(user=self.alice, muted_user=self.alice).exists()
        )

    def test_cannot_block_self(self):
        """L4: self-block is rejected with 400 and creates no row."""
        from api.models import BlockedUser
        resp = self.client.post(
            "/auth/block/", {"user_id": self.alice.id}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(
            BlockedUser.objects.filter(
                user=self.alice, blocked_user=self.alice
            ).exists()
        )
