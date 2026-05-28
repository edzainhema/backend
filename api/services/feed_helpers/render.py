"""Feed output utilities: per-post serialization, recency-decay scoring,
and the two-feed merge interleaver."""
import math

from django.utils import timezone

from ...models import Follow, PageFollow
from ..post_media import ordered_media


def recency_decay(created_at, half_life_hours=24):
    """
    Returns a multiplier between 0 and 1
    """
    age_hours = (
        timezone.now() - created_at
    ).total_seconds() / 3600

    return math.exp(-age_hours / half_life_hours)


def serialize_post(
    post,
    user,
    request,
    *,
    suggested=False,
    top_comments=None
):
    # ---------------- USER AVATAR ----------------
    avatar = None
    profile = getattr(post.user, "userprofile", None)
    if profile and profile.avatar:
        avatar = request.build_absolute_uri(profile.avatar.url)

    # ---------------- PAGE AVATAR ----------------
    page_avatar = None
    if post.page and post.page.avatar:
        page_avatar = request.build_absolute_uri(post.page.avatar.url)

    return {
        "id": post.id,
        "description": post.description,
        "created_at": post.created_at,
        "suggested": suggested,
        "is_public_override": post.is_public_override,

        "user": {
            "id": post.user.id,
            "username": post.user.username,
            "avatar": avatar,
        },

        "page": (
            {
                "id": post.page.id,
                "name": post.page.name,
                "avatar": page_avatar,
                "is_private": post.page.is_private,
            }
            if post.page else None
        ),

        # Prefer annotation values (set by get_followed_feed / get_suggested_feed)
        # to avoid N+1 queries. Fall back to live queries only when the post
        # object comes from a code path that doesn't annotate (e.g. tests).
        "likes_count": getattr(post, "likes_count_ann", None) if getattr(post, "likes_count_ann", None) is not None else post.likes.count(),
        "is_liked": bool(getattr(post, "viewer_liked", None)) if getattr(post, "viewer_liked", None) is not None else post.likes.filter(user=user).exists(),
        "is_owner": post.user_id == user.id,

        "comments_count": getattr(post, "comments_count_ann", None) if getattr(post, "comments_count_ann", None) is not None else post.comments.count(),

        "saves_count": getattr(post, "saves_count_ann", None) if getattr(post, "saves_count_ann", None) is not None else post.saved_by.count(),
        "is_saved": bool(getattr(post, "viewer_saved", None)) if getattr(post, "viewer_saved", None) is not None else post.saved_by.filter(user=user).exists(),

        "is_followed": bool(getattr(post, "viewer_follows_author", None)) if getattr(post, "viewer_follows_author", None) is not None else Follow.objects.filter(follower=user, following=post.user).exists(),
        "is_page_followed": (
            bool(getattr(post, "viewer_follows_page", None)) if getattr(post, "viewer_follows_page", None) is not None
            else PageFollow.objects.filter(user=user, page=post.page).exists()
            if post.page else False
        ),

        "top_comments": top_comments or [],

        # Read media from the `ordered_media` attr (set via Prefetch with
        # to_attr=...) when available; otherwise sort the prefetched cache in
        # Python. Calling `post.media.all().order_by("order")` directly would
        # bust `prefetch_related("media")` and re-query per post — see the
        # same fix in serializers.py FeedPostSerializer.get_media.
        "media": [
            {
                "id": m.id,
                "file": request.build_absolute_uri(m.file.url),
                "thumbnail": (
                    request.build_absolute_uri(m.thumbnail.url)
                    if m.thumbnail else None
                ),
                "order": m.order,
                # Pixel dimensions captured at upload time. Null for legacy
                # rows uploaded before the column existed — the client
                # falls back to Image.getSize / video naturalSize then.
                "width": m.width,
                "height": m.height,
                "tags": [
                    {
                        "id": t.user.id,
                        "username": t.user.username,
                    }
                    for t in m.tags.all()
                ],
            }
            for m in ordered_media(post)
        ],
    }


def merge_feed(primary, secondary, interval=5):
    merged = []
    s_idx = 0

    for i, item in enumerate(primary):
        merged.append(item)

        if i % interval == interval - 1 and s_idx < len(secondary):
            merged.append(secondary[s_idx])
            s_idx += 1

    # When the followed feed is short or empty, append remaining suggested
    # posts so users with small follow graphs still see a full feed.
    while s_idx < len(secondary):
        merged.append(secondary[s_idx])
        s_idx += 1

    return merged


