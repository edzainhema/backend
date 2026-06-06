"""Regression tests for audits H1 + M2: the discovery rails re-apply the
viewer's block/mute/visibility exclusions on a CACHE HIT.

M2's concern: the four per-viewer rails cache their own `[(post_id, score)]`
lists under keys (`feed:friend_network:*`, `feed:nearby:*`,
`feed:activity_scores:*`, `feed:collaborative:*`) that block/mute toggles don't
clear, so a freshly blocked/muted author's posts could keep surfacing for the
rail TTL even after `feed_ctx` was cleared.

The H1 fix resolves this WITHOUT clearing those rail keys: every rail's cache-hit
path now goes through `scoring.rehydrate_visible_slice`, which re-applies
`post_visibility_q` + block/mute/not-interested exclusions against the
(freshly-rebuilt-after-the-toggle) feed context. So clearing `feed_ctx` on
block/mute — which the privacy views already do — is sufficient: the rail
re-derives its exclusions from it on the very next load.

These tests pin that behaviour so a future change to the cache-hit path can't
silently reintroduce the leak.
"""
from django.contrib.auth.models import User
from rest_framework.test import APITestCase

from api.models import Post
from api.feed.scoring import rehydrate_visible_slice


def _ctx(**over):
    base = {
        "followed_users": set(),
        "followed_pages": set(),
        "blocked_user_ids": set(),
        "muted_user_ids": set(),
        "muted_page_ids": set(),
    }
    base.update(over)
    return base


class RailCacheHitExclusionTests(APITestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="viewer", password="x")
        self.author = User.objects.create_user(username="author", password="x")
        # A plain public personal post by `author`.
        self.post = Post.objects.create(user=self.author, description="hi")
        # Stand in for a warm rail score cache built BEFORE the toggle: the
        # author's post id is already in the cached [(post_id, score)] list.
        self.scored = [(self.post.id, 1.0)]

    def test_visible_when_not_excluded(self):
        """Baseline: with empty exclusion sets the cached post is served."""
        out = rehydrate_visible_slice(
            self.scored, user=self.viewer, context=_ctx(),
            exclude_ids=set(), offset=0, limit=10,
        )
        self.assertEqual([pid for pid, _, _ in out], [self.post.id])

    def test_blocked_author_dropped_on_cache_hit(self):
        """Right after viewer blocks author, feed_ctx is rebuilt with author in
        blocked_user_ids — and the rail drops the post even though its id is
        still in the cached score list (the M2 leak, closed by H1)."""
        out = rehydrate_visible_slice(
            self.scored, user=self.viewer,
            context=_ctx(blocked_user_ids={self.author.id}),
            exclude_ids=set(), offset=0, limit=10,
        )
        self.assertEqual(out, [])

    def test_muted_author_dropped_on_cache_hit(self):
        out = rehydrate_visible_slice(
            self.scored, user=self.viewer,
            context=_ctx(muted_user_ids={self.author.id}),
            exclude_ids=set(), offset=0, limit=10,
        )
        self.assertEqual(out, [])

    def test_not_interested_author_dropped_on_cache_hit(self):
        out = rehydrate_visible_slice(
            self.scored, user=self.viewer,
            context=_ctx(not_interested_user_ids={self.author.id}),
            exclude_ids=set(), offset=0, limit=10,
        )
        self.assertEqual(out, [])
