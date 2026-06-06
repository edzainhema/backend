"""Tests for the M8 fix: toggle_block_user invalidates the unread
notification badge cache for BOTH parties on block AND unblock.

Both `list_notifications` and `unread_notifications_count` filter on
the BLOCKED_USER set in either direction, so when alice blocks bob:
  * alice's unread count must drop (bob's notifications to alice are
    now hidden from alice's listing)
  * bob's unread count must drop (alice's notifications to bob are
    now hidden from bob's listing)
The unread count is cached for UNREAD_COUNT_CACHE_TTL_S (30s);
without explicit invalidation the bell badge can stay wrong for
that whole window.

Same logic in reverse for unblock.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import BlockedUser
from api.services.notification_cache import _unread_count_cache_key


def _register(client, username: str) -> str:
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "strong-pass-9999",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


class ToggleBlockUserCacheInvalidationTests(APITestCase):

    def setUp(self):
        # cache.clear() also wipes the throttle bucket so the 3 register
        # calls below stay under the 5/min cap, AND wipes any prior
        # unread-count entries so the seed values in each test start
        # from a clean baseline.
        cache.clear()
        self.alice_access = _register(self.client, "alice")
        self.alice = User.objects.get(username="alice")
        self.bob_access = _register(self.client, "bob")
        self.bob = User.objects.get(username="bob")
        # `bystander` is unrelated -- their cache MUST NOT be touched.
        self.bystander_access = _register(self.client, "bystander")
        self.bystander = User.objects.get(username="bystander")

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _seed_unread_counts(self):
        """Drop sentinel ints into the unread-count cache for all
        three users. After the toggle endpoint runs, alice's + bob's
        should be cleared and bystander's should survive."""
        for uid in (self.alice.id, self.bob.id, self.bystander.id):
            cache.set(_unread_count_cache_key(uid), 99)

    def _toggle_block(self, target):
        return self.client.post(
            "/auth/block/", {"user_id": target.id}, format="json",
        )

    # ---------- block fires invalidation ----------

    def test_block_clears_unread_count_for_both_parties(self):
        self._seed_unread_counts()
        self._auth(self.alice_access)
        resp = self._toggle_block(self.bob)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data.get("status"), "blocked")

        # Both the blocker (alice) AND the target (bob) get their
        # unread-count cache cleared.
        self.assertIsNone(cache.get(_unread_count_cache_key(self.alice.id)))
        self.assertIsNone(cache.get(_unread_count_cache_key(self.bob.id)))
        # Bystander cache untouched.
        self.assertEqual(
            cache.get(_unread_count_cache_key(self.bystander.id)), 99,
        )

    def test_unblock_clears_unread_count_for_both_parties(self):
        # Pre-create the block so toggle_block_user takes the unblock branch.
        BlockedUser.objects.create(user=self.alice, blocked_user=self.bob)
        self._seed_unread_counts()

        self._auth(self.alice_access)
        resp = self._toggle_block(self.bob)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data.get("status"), "unblocked")

        self.assertIsNone(cache.get(_unread_count_cache_key(self.alice.id)))
        self.assertIsNone(cache.get(_unread_count_cache_key(self.bob.id)))
        self.assertEqual(
            cache.get(_unread_count_cache_key(self.bystander.id)), 99,
        )

    # ---------- regression: mute path is unchanged ----------

    def test_mute_does_not_clear_unread_count(self):
        """Sanity: mute doesn't affect the unread-count cache today
        (the notifications listing filters on BLOCKED actors only, not
        muted ones). If a future change starts filtering on mute, this
        test should fail and tell us to mirror M8's invalidation
        pattern in toggle_mute_user."""
        cache.set(_unread_count_cache_key(self.alice.id), 99)
        self._auth(self.alice_access)
        resp = self.client.post(
            "/auth/mute/", {"user_id": self.bob.id}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        # Cache survives mute.
        self.assertEqual(
            cache.get(_unread_count_cache_key(self.alice.id)), 99,
        )
