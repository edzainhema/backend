"""Tests for the H7 fix on /posts/public-override/.

`toggle_post_public` flips `Post.is_public_override`. The flag controls
whether a private-page post leaks into the home feed of the author's
followers (per `post_visibility_q`). Before this fix the view saved
the change and returned, leaving the per-follower
`suggested_feed_scores:*` caches stale for up to 5 minutes -- so a
private-page poster who pushed a post public would watch their
followers' feeds NOT update for several minutes.

These tests assert:
  * Followers' caches are cleared on toggle.
  * Non-followers' caches are NOT cleared (precision).
  * Author's own cache stays untouched (they don't follow themselves).
  * Cache invalidation fires on BOTH directions of the toggle.
  * Ownership / page-type guards still work (regression).
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import Follow, Page, Post


def _register(client, username: str) -> str:
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "strong-pass-9999",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


class PostPublicOverrideCacheTests(APITestCase):
    """H7: the follower-feed cache must be invalidated on toggle."""

    def setUp(self):
        # B4 register throttle is 5/min; cache.clear() also wipes the
        # throttle bucket so this class's 3 register calls don't trip
        # it when run alongside other test classes.
        cache.clear()

        self.author_access = _register(self.client, "author")
        self.author = User.objects.get(username="author")
        self.follower_access = _register(self.client, "follower")
        self.follower = User.objects.get(username="follower")
        # `bystander` exists but doesn't follow `author` -- their cache
        # MUST NOT be cleared, otherwise the invalidation is too broad
        # and wastes computed feeds.
        self.bystander_access = _register(self.client, "bystander")
        self.bystander = User.objects.get(username="bystander")

        Follow.objects.create(follower=self.follower, following=self.author)

        # Private page + post by author. is_public_override starts
        # False; the toggle endpoint requires the post to be in a
        # private (or super-private) page, which makes the flag
        # actually do something visibility-wise.
        self.page = Page.objects.create(
            owner=self.author, name="Secret Club", is_private=True,
        )
        self.post = Post.objects.create(
            user=self.author, page=self.page, description="members only",
        )

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _toggle(self):
        return self.client.post(
            "/posts/public-override/",
            {"post_id": self.post.id},
            format="json",
        )

    # ---------------- cache invalidation ----------------

    def test_toggle_invalidates_follower_feed_cache(self):
        cache.set(f"suggested_feed_scores:{self.follower.id}", "sentinel")
        cache.set(f"suggested_feed_scores:{self.bystander.id}", "bystander_sentinel")
        cache.set(f"suggested_feed_scores:{self.author.id}", "author_sentinel")

        self._auth(self.author_access)
        resp = self._toggle()
        self.assertEqual(resp.status_code, 200, resp.content)

        # Follower's cache is gone -- the next /feed/ load recomputes.
        self.assertIsNone(
            cache.get(f"suggested_feed_scores:{self.follower.id}"),
        )
        # Non-follower's cache is untouched -- they can't see this
        # post anyway, so invalidating their cache would be wasteful.
        self.assertEqual(
            cache.get(f"suggested_feed_scores:{self.bystander.id}"),
            "bystander_sentinel",
        )
        # Author's own cache is untouched -- they don't follow
        # themselves, so the cache the view iterates over (followers
        # of `request.user`) doesn't include them.
        self.assertEqual(
            cache.get(f"suggested_feed_scores:{self.author.id}"),
            "author_sentinel",
        )

    def test_toggle_invalidates_on_both_directions(self):
        """ON -> OFF must invalidate too: a post that WAS visible to
        followers needs to disappear from their cached feed."""
        cache.set(f"suggested_feed_scores:{self.follower.id}", "before_on")
        self._auth(self.author_access)
        self._toggle()  # False -> True
        self.assertIsNone(
            cache.get(f"suggested_feed_scores:{self.follower.id}"),
        )

        cache.set(f"suggested_feed_scores:{self.follower.id}", "before_off")
        self._toggle()  # True -> False
        self.assertIsNone(
            cache.get(f"suggested_feed_scores:{self.follower.id}"),
        )

    def test_toggle_with_no_followers_is_a_noop_on_cache(self):
        """When the author has no followers, there's nothing to
        invalidate -- the cache.delete_many is skipped (empty list).
        Sanity check the endpoint still works."""
        Follow.objects.filter(following=self.author).delete()
        cache.set(f"suggested_feed_scores:{self.bystander.id}", "untouched")

        self._auth(self.author_access)
        resp = self._toggle()
        self.assertEqual(resp.status_code, 200, resp.content)
        # Bystander's cache untouched.
        self.assertEqual(
            cache.get(f"suggested_feed_scores:{self.bystander.id}"),
            "untouched",
        )

    def test_invalidates_caches_for_many_followers(self):
        """Verifies the delete_many batch correctly clears all of
        them in one call, not just the first."""
        extra_followers = []
        for i in range(5):
            other = User.objects.create_user(
                username=f"extra{i}", password="x",
            )
            Follow.objects.create(follower=other, following=self.author)
            extra_followers.append(other)
            cache.set(f"suggested_feed_scores:{other.id}", f"value_{i}")

        # Original follower from setUp too.
        cache.set(f"suggested_feed_scores:{self.follower.id}", "orig")

        self._auth(self.author_access)
        resp = self._toggle()
        self.assertEqual(resp.status_code, 200, resp.content)

        for u in extra_followers + [self.follower]:
            self.assertIsNone(
                cache.get(f"suggested_feed_scores:{u.id}"),
                f"expected cleared cache for follower id={u.id}",
            )

    # ---------------- regression: existing guards still work ----

    def test_flag_actually_flips(self):
        self.post.refresh_from_db()
        self.assertFalse(self.post.is_public_override)

        self._auth(self.author_access)
        resp = self._toggle()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["is_public_override"])
        self.post.refresh_from_db()
        self.assertTrue(self.post.is_public_override)

        resp = self._toggle()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.data["is_public_override"])

    def test_non_owner_cannot_toggle(self):
        self._auth(self.follower_access)
        resp = self._toggle()
        self.assertEqual(resp.status_code, 403)

    def test_public_page_post_cannot_be_toggled(self):
        """Endpoint only makes sense for posts in private / super-private
        pages -- posts on a public page are already visible to everyone."""
        public_page = Page.objects.create(
            owner=self.author, name="Open", is_private=False,
        )
        public_post = Post.objects.create(
            user=self.author, page=public_page, description="open",
        )

        self._auth(self.author_access)
        resp = self.client.post(
            "/posts/public-override/",
            {"post_id": public_post.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_missing_post_id_returns_400(self):
        self._auth(self.author_access)
        resp = self.client.post(
            "/posts/public-override/", {}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
