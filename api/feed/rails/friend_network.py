"""Friend-network rail: posts from authors followed by the viewer's mutuals (and, weighted higher, their close friends)."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from ...models import Follow, Post
from ...services.feed_helpers import post_visibility_q
from ..constants import FRIEND_NETWORK_CLOSE_WEIGHT, FRIEND_NETWORK_HALF_LIFE_DAYS, FRIEND_NETWORK_MIN_MUTUAL, FRIEND_NETWORK_MIN_SCORE, FRIEND_NETWORK_POOL, FRIEND_NETWORK_TTL_S, FRIEND_NETWORK_WINDOW_DAYS
from ..scoring import _annotate_for_serialize, _engagement_score, _exclude_not_interested, recency_decay_days

# =============================================================================
# RAIL (b) — Friend-network
# =============================================================================

def _rail_friend_network(request, user, context, *, offset: int, limit: int,
                         exclude_ids: set[int]):
    """
    Returns [(post_id, score, post_obj), ...] for posts from non-followed
    authors with strong mutual-graph overlap with the viewer. Sorted
    best-first. The list is offset+limit long if the cache hit; otherwise
    the full scored list is computed once and cached.
    """
    cache_key = f"feed:friend_network:{user.id}"
    scored = cache.get(cache_key)

    viewer_followers = context["viewer_followers"]
    viewer_following = context["viewer_following"]
    # Close friends (DM / tag / comment / like-weighted), used to heavily
    # up-weight an author whom the viewer's CLOSE friends follow — a far
    # stronger endorsement than the same number of random mutuals (B8).
    close_friends = context.get("close_friend_ids") or set()

    if scored is None:
        candidates = (
            Post.objects
            .filter(post_visibility_q(
                user, context["followed_users"], context["followed_pages"],
            ))
            .exclude(user_id__in=context["followed_users"])
            .exclude(user_id=user.id)
            .exclude(user_id__in=context["blocked_user_ids"])
            .exclude(user_id__in=context["muted_user_ids"])
            .exclude(page_id__in=context["muted_page_ids"])
            .filter(
                created_at__gte=timezone.now() - timedelta(days=FRIEND_NETWORK_WINDOW_DAYS)
            )
            .distinct()
        )
        candidates = _exclude_not_interested(candidates, context)
        candidates = _annotate_for_serialize(candidates, user).order_by("-created_at")[:FRIEND_NETWORK_POOL]
        candidates = list(candidates)

        candidate_user_ids = list({p.user_id for p in candidates})

        # Batch-load the social graph for all candidate authors in two queries.
        author_followers_map: dict[int, set] = defaultdict(set)
        for follower_id, following_id in (
            Follow.objects
            .filter(following_id__in=candidate_user_ids)
            .values_list("follower_id", "following_id")
        ):
            author_followers_map[following_id].add(follower_id)

        author_following_map: dict[int, set] = defaultdict(set)
        for follower_id, following_id in (
            Follow.objects
            .filter(follower_id__in=candidate_user_ids)
            .values_list("follower_id", "following_id")
        ):
            author_following_map[follower_id].add(following_id)

        scored_pairs = []
        for p in candidates:
            a_followers = author_followers_map[p.user_id]
            a_following = author_following_map[p.user_id]
            mutual_followers = len(viewer_followers & a_followers)
            mutual_following = len(viewer_following & a_following)

            if mutual_followers + mutual_following < FRIEND_NETWORK_MIN_MUTUAL:
                continue

            # B8: how many of the viewer's CLOSE friends are in the overlap —
            # close friends who follow the author, plus close friends the
            # author follows. Each is worth a large bonus on top of the plain
            # mutual weight, so an author endorsed by a few close friends
            # decisively out-ranks one backed by the same number of randoms.
            close_overlap = (
                len(close_friends & a_followers)
                + len(close_friends & a_following)
            )

            social = (
                mutual_followers * 3
                + mutual_following * 2
                + close_overlap * FRIEND_NETWORK_CLOSE_WEIGHT
            )
            engagement = _engagement_score(
                p.likes_count_ann, p.comments_count_ann, p.saves_count_ann,
                p.impression_count,
            )
            score = (social + 2 * engagement) * recency_decay_days(
                p.created_at, FRIEND_NETWORK_HALF_LIFE_DAYS
            )

            if score >= FRIEND_NETWORK_MIN_SCORE:
                scored_pairs.append((p.id, score))

        scored_pairs.sort(key=lambda x: x[1], reverse=True)
        # Cache just the id+score list; we re-fetch annotated objects below
        # for the slice we actually need.
        scored = scored_pairs
        cache.set(cache_key, scored, timeout=FRIEND_NETWORK_TTL_S)

        # On a cache miss we already have the full candidate list in memory —
        # build the result for *this page* from it directly.
        post_map = {p.id: p for p in candidates}
        out = []
        for pid, score in scored:
            if pid in exclude_ids:
                continue
            if pid not in post_map:
                continue
            out.append((pid, score, post_map[pid]))
            if len(out) >= offset + limit:
                break
        return out[offset:offset + limit]

    # Cache hit — fetch the slice we need from the DB.
    wanted_ids = [
        pid for pid, _ in scored
        if pid not in exclude_ids
    ][offset:offset + limit]
    if not wanted_ids:
        return []

    posts = list(
        _annotate_for_serialize(Post.objects.filter(id__in=wanted_ids), user)
    )
    post_map = {p.id: p for p in posts}
    score_map = dict(scored)
    return [
        (pid, score_map[pid], post_map[pid])
        for pid in wanted_ids
        if pid in post_map
    ]
