"""Regression test for audit M4/F2: bounding the friend-network rail's
follow-edge loads must NOT drop close-friend endorsements.

The rail now filters the per-author follower/following loads in the DB to the
viewer's graph instead of pulling a celebrity's entire follower list into
memory. The safe filter is `viewer_followers | close_friends` (and the mirror)
— NOT `viewer_followers` alone — because `close_friends` is DM/tag/comment/like
derived and is not a subset of the follow graph. If the union were wrong, an
author followed by one of the viewer's close friends (who isn't otherwise a
follower) would lose the close-friend score bonus.

This test pins that: the same author scores HIGHER when a close friend follows
them than when they don't, proving the close-friend edge is still loaded and
counted after the bounding optimization.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import Follow, Post
from api.feed.rails.friend_network import _rail_friend_network


def _context(viewer, *, followers, following, close_friends):
    return {
        "followed_users": set(following),
        "followed_pages": set(),
        "blocked_user_ids": set(),
        "muted_user_ids": set(),
        "muted_page_ids": set(),
        "viewer_followers": set(followers),
        "viewer_following": set(following),
        "close_friend_ids": set(close_friends),
    }


class FriendNetworkCloseFriendBoundingTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.viewer = User.objects.create_user(username="viewer", password="x")
        self.author = User.objects.create_user(username="author", password="x")
        # Three mutuals that follow BOTH the viewer and the author — enough to
        # clear FRIEND_NETWORK_MIN_MUTUAL (3) so the author is scored at all.
        self.f1 = User.objects.create_user(username="f1", password="x")
        self.f2 = User.objects.create_user(username="f2", password="x")
        self.f3 = User.objects.create_user(username="f3", password="x")
        # A close friend who follows the author but is NOT in the viewer's
        # follower/following graph — the edge the naive (followers-only) filter
        # would have dropped.
        self.close = User.objects.create_user(username="close", password="x")

        for u in (self.f1, self.f2, self.f3):
            Follow.objects.create(follower=u, following=self.viewer)  # viewer_follower
            Follow.objects.create(follower=u, following=self.author)  # follows author
        Follow.objects.create(follower=self.close, following=self.author)

        self.post = Post.objects.create(user=self.author, description="hi")
        self.followers = {self.f1.id, self.f2.id, self.f3.id}

    def _score_for_author(self, close_friends):
        cache.clear()  # force a cache MISS so the rail recomputes the load
        ctx = _context(
            self.viewer,
            followers=self.followers,
            following=set(),
            close_friends=close_friends,
        )
        out = _rail_friend_network(
            None, self.viewer, ctx, offset=0, limit=10, exclude_ids=set(),
        )
        scores = {pid: score for pid, score, _ in out}
        return scores.get(self.post.id)

    def test_close_friend_follow_boosts_score(self):
        without = self._score_for_author(close_friends=set())
        with_close = self._score_for_author(close_friends={self.close.id})

        # Both clear the mutual gate (3 mutual followers), so both are scored.
        self.assertIsNotNone(without, "author should score on mutuals alone")
        self.assertIsNotNone(with_close)
        # The close-friend endorsement must raise the score — proving the
        # close-friend edge survived the DB-side bounding (M4 safe fix). The
        # naive followers-only filter would have dropped `close` and produced
        # equal scores.
        self.assertGreater(with_close, without)
