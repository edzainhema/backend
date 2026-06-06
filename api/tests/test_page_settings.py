"""Tests for /pages/settings/ (`update_page_settings`).

Focused on the H6 fix: when the page owner flips a visibility-affecting
flag (`is_private`, `is_super_private`, `chat_enabled`), the per-viewer
feed caches for the owner and every current follower must be cleared so
the visibility change lands in their feeds on the next /feed/ load
instead of waiting out the 90s / 5min TTLs.

Also smoke-covers the ownership and key-validation guards (regression
guard so the H6 invalidation block doesn't accidentally fire for
non-owners or for invalid keys).
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import Page, PageFollow


def _register(client, username: str) -> str:
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "strong-pass-9999",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


class UpdatePageSettingsCacheInvalidationTests(APITestCase):
    """H6: flipping `is_private` / `is_super_private` / `chat_enabled`
    invalidates the per-viewer feed caches for owner + all followers,
    NOT for bystanders. Other keys (description / anyone_can_post /
    is_event / event_*) must NOT trigger the invalidation."""

    def setUp(self):
        cache.clear()
        self.owner_access = _register(self.client, "owner")
        self.owner = User.objects.get(username="owner")
        self.follower_access = _register(self.client, "follower")
        self.follower = User.objects.get(username="follower")
        # bystander does NOT follow the page -- their cache must stay
        # untouched, otherwise the invalidation is too broad.
        self.bystander_access = _register(self.client, "bystander")
        self.bystander = User.objects.get(username="bystander")

        self.page = Page.objects.create(
            owner=self.owner, name="My Page", is_private=False,
        )
        PageFollow.objects.create(user=self.follower, page=self.page)

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _settings(self, key, value):
        return self.client.post(
            "/pages/settings/",
            {"page_id": self.page.id, "key": key, "value": value},
            format="json",
        )

    def _seed_caches(self):
        """Drop sentinel values into the caches we expect the view to
        invalidate, plus the bystander's caches which must survive."""
        for uid in (self.owner.id, self.follower.id, self.bystander.id):
            cache.set(f"feed_ctx:{uid}", f"ctx-{uid}")
            cache.set(f"suggested_feed_scores:{uid}", f"scores-{uid}")

    def _assert_caches_cleared_for(self, uids):
        for uid in uids:
            self.assertIsNone(
                cache.get(f"feed_ctx:{uid}"),
                f"feed_ctx:{uid} should have been cleared",
            )
            self.assertIsNone(
                cache.get(f"suggested_feed_scores:{uid}"),
                f"suggested_feed_scores:{uid} should have been cleared",
            )

    def _assert_caches_intact_for(self, uids):
        for uid in uids:
            self.assertEqual(cache.get(f"feed_ctx:{uid}"), f"ctx-{uid}")
            self.assertEqual(
                cache.get(f"suggested_feed_scores:{uid}"), f"scores-{uid}",
            )

    # ---------- visibility-affecting flips invalidate ------------

    def test_is_super_private_flip_invalidates_owner_and_followers(self):
        self._seed_caches()
        self._auth(self.owner_access)
        resp = self._settings("is_super_private", True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self._assert_caches_cleared_for([self.owner.id, self.follower.id])
        self._assert_caches_intact_for([self.bystander.id])

    def test_is_private_flip_invalidates_owner_and_followers(self):
        self._seed_caches()
        self._auth(self.owner_access)
        resp = self._settings("is_private", True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self._assert_caches_cleared_for([self.owner.id, self.follower.id])
        self._assert_caches_intact_for([self.bystander.id])

    def test_chat_enabled_flip_invalidates_owner_and_followers(self):
        self._seed_caches()
        self._auth(self.owner_access)
        resp = self._settings("chat_enabled", True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self._assert_caches_cleared_for([self.owner.id, self.follower.id])
        self._assert_caches_intact_for([self.bystander.id])

    def test_super_private_to_public_invalidates_caches(self):
        """The reverse direction (super-private -> public) matters too:
        followers who weren't being shown this page's posts should now
        see them on the next feed load. Verifies invalidation fires on
        BOTH directions of the toggle."""
        self.page.is_super_private = True
        self.page.save(update_fields=["is_super_private"])

        self._seed_caches()
        self._auth(self.owner_access)
        resp = self._settings("is_super_private", False)
        self.assertEqual(resp.status_code, 200, resp.content)
        self._assert_caches_cleared_for([self.owner.id, self.follower.id])
        self._assert_caches_intact_for([self.bystander.id])

    # ---------- non-visibility keys do NOT invalidate -----------

    def test_description_change_does_not_invalidate_caches(self):
        self._seed_caches()
        self._auth(self.owner_access)
        resp = self._settings("description", "new description")
        self.assertEqual(resp.status_code, 200, resp.content)
        # All caches untouched -- the page's content didn't gain or
        # lose any viewers.
        self._assert_caches_intact_for(
            [self.owner.id, self.follower.id, self.bystander.id],
        )

    def test_anyone_can_post_change_does_not_invalidate_caches(self):
        """anyone_can_post governs WHO CAN POST, not who can see -- the
        feed-visibility caches don't change shape when it flips."""
        self._seed_caches()
        self._auth(self.owner_access)
        resp = self._settings("anyone_can_post", True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self._assert_caches_intact_for(
            [self.owner.id, self.follower.id, self.bystander.id],
        )

    def test_is_event_change_does_not_invalidate_caches(self):
        """is_event is a display-category flag; doesn't affect visibility."""
        self._seed_caches()
        self._auth(self.owner_access)
        resp = self._settings("is_event", True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self._assert_caches_intact_for(
            [self.owner.id, self.follower.id, self.bystander.id],
        )

    # ---------- regression: existing guards still fire ----------

    def test_non_owner_cannot_change_settings(self):
        """Sanity guard: a follower trying to flip privacy is rejected
        BEFORE the cache invalidation runs -- otherwise an attacker
        with a feed cache could trigger arbitrary cache deletes."""
        self._seed_caches()
        self._auth(self.follower_access)
        resp = self._settings("is_super_private", True)
        self.assertEqual(resp.status_code, 403)
        # Caches untouched -- we 403'd before reaching the H6 block.
        self._assert_caches_intact_for(
            [self.owner.id, self.follower.id, self.bystander.id],
        )

    def test_invalid_key_returns_400_and_no_invalidation(self):
        self._seed_caches()
        self._auth(self.owner_access)
        resp = self._settings("definitely_not_a_real_key", True)
        self.assertEqual(resp.status_code, 400)
        self._assert_caches_intact_for(
            [self.owner.id, self.follower.id, self.bystander.id],
        )

    def test_invalid_bool_value_returns_400_and_no_invalidation(self):
        """Sanity: type validation must run BEFORE the cache delete,
        so a rejected request doesn't accidentally clear feed state."""
        self._seed_caches()
        self._auth(self.owner_access)
        resp = self._settings("is_super_private", "not-a-bool")
        self.assertEqual(resp.status_code, 400)
        self._assert_caches_intact_for(
            [self.owner.id, self.follower.id, self.bystander.id],
        )
