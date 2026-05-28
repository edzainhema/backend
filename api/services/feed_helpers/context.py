"""Cached per-user feed context: muted pages, social sets, blocked-user
ids, etc. Built once per request via build_feed_context (the cached wrapper)."""
from django.core.cache import cache

from ...models import BlockedUser, Follow, MutedUser, NotInterested, PageFollow

from .visibility import get_muted_page_ids
from .social import get_close_friend_ids, get_friend_ids


def _build_feed_context_uncached(user):
    """
    Compute all shared sets used across feed logic (no caching).
    Called only by build_feed_context — do not call directly.
    """

    # ---------------- BLOCKED USERS (both directions) ----------------
    blocked_pairs = BlockedUser.objects.involving(user).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_user_ids = set()
    for u, b in blocked_pairs:
        blocked_user_ids.add(u)
        blocked_user_ids.add(b)
    blocked_user_ids.discard(user.id)

    # Single query — reused for both "followed_users" and "viewer_following"
    # (they are the same set; previously two identical DB round-trips).
    followed_user_ids = set(
        Follow.objects.filter(
            follower=user
        ).values_list("following_id", flat=True)
    )

    viewer_followers = set(
        Follow.objects.filter(
            following=user
        ).values_list("follower_id", flat=True)
    )

    # ---------------- NOT-INTERESTED EXCLUSIONS ----------------
    # The viewer's explicit "show me less" choices (B2). One query, ordered
    # newest-first and bounded, then partitioned by kind. Author/topic sets
    # are naturally tiny; the cap only guards against a user who has dismissed
    # thousands of individual posts.
    not_interested_post_ids: set = set()
    not_interested_user_ids: set = set()
    not_interested_hashtags: set = set()
    for kind, pid, tuid, tag in (
        NotInterested.objects
        .filter(user=user)
        .order_by("-created_at")
        .values_list("kind", "post_id", "target_user_id", "hashtag")[:5000]
    ):
        if kind == NotInterested.KIND_POST and pid is not None:
            not_interested_post_ids.add(pid)
        elif kind == NotInterested.KIND_AUTHOR and tuid is not None:
            not_interested_user_ids.add(tuid)
        elif kind == NotInterested.KIND_TOPIC and tag:
            not_interested_hashtags.add(tag)

    return {
        "blocked_user_ids": blocked_user_ids,

        "not_interested_post_ids": not_interested_post_ids,
        "not_interested_user_ids": not_interested_user_ids,
        "not_interested_hashtags": not_interested_hashtags,

        "muted_user_ids": set(
            MutedUser.objects.filter(
                user=user
            ).values_list("muted_user_id", flat=True)
        ),

        "muted_page_ids": set(get_muted_page_ids(user)),

        "followed_users": followed_user_ids,

        "followed_pages": set(
            PageFollow.objects.filter(
                user=user
            ).values_list("page_id", flat=True)
        ),

        "very_close_friend_ids": get_friend_ids(user),

        # The genuine "very close friends" set — ranked by DMs, tags,
        # comments, and likes between the two people (get_very_close_friend_ids,
        # NOT plain mutuals). This was computed nowhere in the live ranking
        # path until now; the friend-network rail uses it to heavily weight an
        # author followed by the viewer's close friends (B8). Cached with the
        # rest of the context (90 s), so its handful of extra queries run at
        # most once per 90 s per viewer.
        "close_friend_ids": get_close_friend_ids(user),

        # Reuse the set computed above — no extra query.
        "viewer_following": followed_user_ids,

        "viewer_followers": viewer_followers,
    }


def build_feed_context(user):
    """
    Return the per-user sets needed by feed queries.
    Results are cached per user for 90 s and invalidated on
    block / mute / follow actions so changes take effect quickly.
    """
    cache_key = f"feed_ctx:{user.id}"
    ctx = cache.get(cache_key)
    if ctx is None:
        ctx = _build_feed_context_uncached(user)
        cache.set(cache_key, ctx, timeout=90)
    return ctx


