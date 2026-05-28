"""Post / content search (`search_posts`) with cursor pagination."""


from django.db.models import Count, Q
from django.utils.dateparse import parse_datetime
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import (
    BlockedUser, Follow, MutedUser, Page, PageFollow, Post, PostLike, SavedPost,
)
from ...services.pagination import decode_cursor, encode_cursor
from ...services.post_media import ordered_media
from ...services.feed_helpers import (
    get_muted_page_ids, post_visibility_q,
    likes_count_subquery, comments_count_subquery, saves_count_subquery,
)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def search_posts(request):
    user = request.user
    q = request.query_params.get("q", "").strip()

    if not q:
        return Response({"results": [], "has_more": False, "next_cursor": None})

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    # --- blocked / muted guards (same as explore) ---
    blocked_pairs = BlockedUser.objects.involving(user).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_ids = set()
    for u, b in blocked_pairs:
        blocked_ids.add(u)
        blocked_ids.add(b)
    blocked_ids.discard(user.id)

    muted_user_ids = MutedUser.objects.filter(
        user=user
    ).values_list("muted_user_id", flat=True)

    muted_page_ids = get_muted_page_ids(user)

    # Followed users / pages — feed into the canonical visibility filter.
    # Pages the viewer owns are treated as followed so owners always see
    # their own page's posts in search results.
    followed_user_ids = set(
        Follow.objects.filter(follower=user).values_list("following_id", flat=True)
    )
    followed_page_ids = set(
        PageFollow.objects.filter(user=user).values_list("page_id", flat=True)
    ) | set(
        Page.objects.filter(owner=user).values_list("id", flat=True)
    )

    liked_post_ids = set(
        PostLike.objects.filter(user=user).values_list("post_id", flat=True)
    )
    saved_post_ids = set(
        SavedPost.objects.filter(user=user).values_list("post_id", flat=True)
    )

    # Match on: post description · author username · page name
    posts = (
        Post.objects
        .filter(
            Q(description__icontains=q)
            | Q(user__username__icontains=q)
            | Q(page__name__icontains=q)
        )
        # Canonical visibility filter — replaces an earlier rule that only
        # excluded `is_private` pages, leaving super-private pages and
        # private-account personal posts visible in search results.
        .filter(post_visibility_q(user, followed_user_ids, followed_page_ids))
        .exclude(user_id__in=blocked_ids)
        .exclude(user_id__in=muted_user_ids)
        .exclude(page_id__in=muted_page_ids)
        .annotate(
            media_count=Count("media", distinct=True),
            likes_count=likes_count_subquery(),
            comments_count=comments_count_subquery(),
            saves_count=saves_count_subquery(),
        )
        .filter(media_count__gt=0)
        .distinct()
        .select_related("user", "user__userprofile", "page")
        .prefetch_related("media")
        .order_by("-created_at", "-id")
    )

    # Keyset: posts strictly older than the cursor. The compound (created_at,
    # id) comparison keeps ordering total/stable when posts share a timestamp,
    # so no post is skipped or repeated across pages.
    cursor = decode_cursor(request.query_params.get("cursor"))
    last_created = parse_datetime(cursor["created_at"]) if cursor.get("created_at") else None
    last_id = cursor.get("id")
    if last_created is not None and last_id is not None:
        posts = posts.filter(
            Q(created_at__lt=last_created)
            | Q(created_at=last_created, id__lt=last_id)
        )

    # Fetch one extra row to detect `has_more` without a second COUNT query.
    posts = list(posts[: limit + 1])
    has_more = len(posts) > limit
    posts = posts[:limit]

    result = []
    for post in posts:
        media_qs = ordered_media(post)
        first = media_qs[0]

        is_single_video = (
            len(media_qs) == 1
            and first.file.name.lower().endswith((".mp4", ".mov", ".webm"))
        )

        up = getattr(post.user, "userprofile", None)

        result.append({
            "id": post.id,
            "description": post.description,
            "created_at": post.created_at,
            "is_single_video": is_single_video,
            "media": [
                request.build_absolute_uri(
                    m.thumbnail.url if m.thumbnail else m.file.url
                )
                for m in media_qs
            ],
            "video": request.build_absolute_uri(first.file.url),
            "user": {
                "id": post.user.id,
                "username": post.user.username,
                "avatar": (
                    request.build_absolute_uri(up.avatar.url)
                    if up and up.avatar
                    else None
                ),
            },
            "page": (
                {
                    "id": post.page.id,
                    "name": post.page.name,
                    "avatar": (
                        request.build_absolute_uri(post.page.avatar.url)
                        if post.page.avatar
                        else None
                    ),
                }
                if post.page
                else None
            ),
            "likes_count": post.likes_count,
            "comments_count": post.comments_count,
            "saves_count": post.saves_count,
            "is_liked": post.id in liked_post_ids,
            "is_saved": post.id in saved_post_ids,
        })

    next_cursor = None
    if has_more and posts:
        last = posts[-1]
        next_cursor = encode_cursor({
            "created_at": last.created_at.isoformat(),
            "id": last.id,
        })

    return Response({
        "results": result,
        "has_more": has_more,
        "next_cursor": next_cursor,
    })


