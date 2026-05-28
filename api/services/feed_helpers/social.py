"""Social graph queries: very-close / close / friend ID sets, the combined
`get_social_sets`, and the viewer<->author overlap score."""
from collections import defaultdict
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from ...models import (
    Comment, Conversation, Follow, Message, PostLike, PostMediaTag,
)


def get_very_close_friend_ids(user, limit=15):
    """
    Returns a SET of user IDs ranked by relationship strength
    """

    now = timezone.now()
    since_30d = now - timedelta(days=30)

    scores = defaultdict(int)

    # --------------------------------------------------
    # 👯 Mutual follows (baseline)
    # --------------------------------------------------
    following = set(
        Follow.objects.filter(follower=user)
        .values_list("following_id", flat=True)
    )

    followers = set(
        Follow.objects.filter(following=user)
        .values_list("follower_id", flat=True)
    )

    mutuals = following & followers

    for uid in mutuals:
        scores[uid] += 10   # baseline weight

    # --------------------------------------------------
    # 💬 Recent DMs (very strong signal)
    # --------------------------------------------------
    messages = list(Message.objects.filter(
        Q(sender=user) | Q(conversation__participants=user),
        created_at__gte=since_30d
    ).select_related("sender"))

    # Batch-fetch the other participant for every conversation where the
    # current user is the sender.  Previously this hit the DB once per
    # message (N+1) and crashed with AttributeError when a conversation
    # had no other participants (.first() returned None).
    sender_conv_ids = {
        m.conversation_id for m in messages if m.sender_id == user.id
    }
    conv_other_map: dict = {}
    if sender_conv_ids:
        for conv_id, participant_id in (
            Conversation.objects
            .filter(id__in=sender_conv_ids)
            .values_list("id", "participants")
        ):
            # Keep only the first non-viewer participant per conversation.
            if participant_id != user.id and conv_id not in conv_other_map:
                conv_other_map[conv_id] = participant_id

    for m in messages:
        if m.sender_id != user.id:
            other = m.sender_id
        else:
            other = conv_other_map.get(m.conversation_id)
            if other is None:
                continue  # no other participant found — skip safely
        scores[other] += 5

    # --------------------------------------------------
    # 🏷️ Tagging in posts (strong signal)
    # --------------------------------------------------
    tags = PostMediaTag.objects.filter(
        media__post__user=user,
        created_at__gte=since_30d
    ).values_list("user_id", flat=True)

    for uid in tags:
        scores[uid] += 4

    # --------------------------------------------------
    # 💬 Commenting on each other
    # --------------------------------------------------
    comments = Comment.objects.filter(
        Q(user=user) | Q(post__user=user),
        created_at__gte=since_30d,
        is_deleted=False
    ).select_related("user", "post")

    for c in comments:
        other = c.user_id if c.user_id != user.id else c.post.user_id
        scores[other] += 2

    # --------------------------------------------------
    # ❤️ Likes on each other’s posts (weak)
    # --------------------------------------------------
    likes = PostLike.objects.filter(
        Q(user=user) | Q(post__user=user),
        created_at__gte=since_30d
    )

    for l in likes:
        other = l.user_id if l.user_id != user.id else l.post.user_id
        scores[other] += 1

    # --------------------------------------------------
    # 🔥 Sort + return top N
    # --------------------------------------------------
    ranked = sorted(
        scores.items(),
        key=lambda x: x[1],
        reverse=True
    )

    return {
        user_id
        for user_id, score in ranked[:limit]
        if score >= 10   # threshold
    }


def get_close_friend_ids(user):
    """
    Return the user's precomputed "very close friends" set (UB-1).

    Read path: the expensive 30-day relationship scan (get_very_close_friend_ids)
    moved to the nightly `build_close_friends` job, which writes each active
    user's set to UserCloseFriends. Here we read that row -- one indexed lookup,
    no scan. If there's no row yet (a brand-new user, or before the first
    nightly run) we fall back to computing it once and persisting it, mirroring
    how api.feed.affinity._build_activity_profile handles a missing profile. So
    an established user never triggers the scan on a request; build_feed_context's
    90s cache covers repeated reads within a window.

    An existing-but-empty row ([]) is authoritative (the user genuinely has no
    close friends) and does NOT trigger a recompute -- only a missing row does.
    """
    from ...models import UserCloseFriends

    row = (
        UserCloseFriends.objects
        .filter(user=user)
        .values_list("friend_ids", flat=True)
        .first()
    )
    if row is not None:
        return set(row)

    # No precomputed row yet: compute once, persist, return (bounded -- this
    # only fires before the user's first nightly build).
    ids = get_very_close_friend_ids(user)
    try:
        UserCloseFriends.objects.update_or_create(
            user=user, defaults={"friend_ids": sorted(ids)}
        )
    except Exception:   # persistence is best-effort, never break the feed
        pass
    return ids


def get_friend_ids(user):
    """
    Friends = mutual followers
    """
    following = Follow.objects.filter(
        follower=user
    ).values_list("following_id", flat=True)

    followers = Follow.objects.filter(
        following=user
    ).values_list("follower_id", flat=True)

    return set(following).intersection(set(followers))


def get_social_sets(user):
    """
    Returns all relevant social relationship ID sets for ranking.
    """

    following = set(
        Follow.objects.filter(
            follower=user
        ).values_list("following_id", flat=True)
    )

    followers = set(
        Follow.objects.filter(
            following=user
        ).values_list("follower_id", flat=True)
    )

    friends = following & followers  # mutuals

    following_only = following - friends
    followers_only = followers - friends

    return {
        "friends": friends,
        "following_only": following_only,
        "followers_only": followers_only,
    }


def get_social_overlap_score(viewer, author):
    """
    Measures how socially close `author` is to `viewer`
    """

    viewer_following = set(
        Follow.objects.filter(
            follower=viewer
        ).values_list("following_id", flat=True)
    )

    viewer_followers = set(
        Follow.objects.filter(
            following=viewer
        ).values_list("follower_id", flat=True)
    )

    author_following = set(
        Follow.objects.filter(
            follower=author
        ).values_list("following_id", flat=True)
    )

    author_followers = set(
        Follow.objects.filter(
            following=author
        ).values_list("follower_id", flat=True)
    )

    mutual_followers = len(viewer_followers & author_followers)
    mutual_following = len(viewer_following & author_following)

    return {
        "mutual_followers": mutual_followers,
        "mutual_following": mutual_following,
        "score": (mutual_followers * 3) + (mutual_following * 2)
    }


