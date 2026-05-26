

from django.db.models import (
    Count, Exists, OuterRef, Q,
)

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response



from ..models import (
    BlockedUser, Follow, MutedUser, PageFollow, Post, PostLike,
    PostMedia, SavedPost,
)
from ..services.feed_helpers import (
    get_muted_page_ids, post_visibility_q,
    likes_count_subquery, comments_count_subquery, saves_count_subquery,
)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def reels_feed(request):
    user = request.user

    # --------------------------------------------------
    # PAGINATION
    # Default page size matches what the client renders before fetching more.
    # Both values are clamped so a malformed/abusive client can't ask for
    # an unbounded slice (the original endpoint had no limit at all, which
    # let it scan every reel in the database on every request).
    # --------------------------------------------------
    try:
        limit = int(request.query_params.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 30))
    offset = max(0, offset)

    # --------------------------------------------------
    # BLOCKED USERS (both directions)
    # --------------------------------------------------
    blocked_pairs = BlockedUser.objects.involving(user).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_user_ids = set()
    for u, b in blocked_pairs:
        blocked_user_ids.add(u)
        blocked_user_ids.add(b)

    blocked_user_ids.discard(user.id)

    # --------------------------------------------------
    # MUTED USERS (one-directional)
    # --------------------------------------------------
    muted_user_ids = MutedUser.objects.filter(
        user=user
    ).values_list("muted_user_id", flat=True)

    # --------------------------------------------------
    # MUTED PAGES (one-directional)
    # --------------------------------------------------
    muted_page_ids = get_muted_page_ids(user)

    # --------------------------------------------------
    # FOLLOWED USERS / PAGES (needed by post_visibility_q)
    # --------------------------------------------------
    followed_user_ids = set(
        Follow.objects.filter(follower=user).values_list("following_id", flat=True)
    )
    followed_page_ids = set(
        PageFollow.objects.filter(user=user).values_list("page_id", flat=True)
    )

    # --------------------------------------------------
    # ---------------- FETCH REEL POSTS ----------------
    # Only posts with EXACTLY 1 media item, and that media must be a video.
    #
    # The video-extension check used to live in Python -- we'd fetch the post,
    # inspect `media.file.name`, and `continue` if it wasn't a video. That
    # silently dropped photo posts from the page after the DB had already done
    # the work of returning them, which both wasted query work and broke
    # pagination (a "page of 10" could return 2 reels). Moving the check into
    # SQL via an Exists subquery means the database only returns posts whose
    # one media item is a video, so the page size the client asks for is the
    # page size it actually gets.
    # --------------------------------------------------
    video_media_exists = PostMedia.objects.filter(
        post=OuterRef("pk"),
    ).filter(
        Q(file__iendswith=".mp4")
        | Q(file__iendswith=".mov")
        | Q(file__iendswith=".webm")
    )

    # Per-viewer flags computed in SQL via correlated subqueries -- same
    # pattern as get_page_detail. Without these, the serializer loop below
    # would fire 5 extra queries per post (likes_count, is_liked,
    # comments_count, saves_count, is_saved) and on an unpaginated endpoint
    # that meant ~5 * every-reel-in-the-database queries per request.
    viewer_liked = PostLike.objects.filter(
        post=OuterRef("pk"), user=user,
    )
    viewer_saved = SavedPost.objects.filter(
        post=OuterRef("pk"), user=user,
    )

    posts_qs = (
        Post.objects
        .annotate(media_count=Count("media", distinct=True))
        .filter(media_count=1)
        .annotate(_is_video=Exists(video_media_exists))
        .filter(_is_video=True)
        # Canonical visibility filter — replaces an earlier hand-rolled
        # condition that let posts from super-private pages and from
        # private-profile authors leak through to non-followers.
        .filter(post_visibility_q(user, followed_user_ids, followed_page_ids))
        .exclude(user_id__in=blocked_user_ids)
        .exclude(user_id__in=muted_user_ids)
        .exclude(page_id__in=muted_page_ids)
        .select_related(
            "user",
            "user__userprofile",
            "page",
        )
        .prefetch_related("media")
        .annotate(
            _likes=likes_count_subquery(),
            _comments=comments_count_subquery(),
            _saves=saves_count_subquery(),
            _is_liked=Exists(viewer_liked),
            _is_saved=Exists(viewer_saved),
        )
        .order_by("-created_at", "-id")
    )

    # limit+1 trick -- fetch one extra row so we can tell the client whether
    # more pages exist without firing a separate COUNT(*) on the full table.
    # Same approach used in get_page_detail.
    fetched = list(posts_qs[offset:offset + limit + 1])
    has_more = len(fetched) > limit
    posts = fetched[:limit]

    data = []
    for post in posts:
        media = post.media.first()
        if not media:
            # Defensive: the SQL Exists check above guarantees the post has a
            # video media row, but if media has been deleted between the query
            # and serialization, skip instead of crashing on `media.file.url`.
            continue

        data.append({
            "id": post.id,
            "description": post.description,
            "created_at": post.created_at,
            "is_owner": post.user_id == user.id,
            "is_public_override": post.is_public_override,

            "video": request.build_absolute_uri(media.file.url),

            "user": {
                "id": post.user.id,
                "username": post.user.username,
                "avatar": (
                    request.build_absolute_uri(
                        post.user.userprofile.avatar.url
                    )
                    if hasattr(post.user, "userprofile")
                    and post.user.userprofile.avatar
                    else None
                ),
            },

            "page": (
                {
                    "id": post.page.id,
                    "name": post.page.name,
                    "is_private": post.page.is_private,
                    "avatar": (
                        request.build_absolute_uri(post.page.avatar.url)
                        if post.page.avatar
                        else None
                    ),
                }
                if post.page
                else None
            ),

            "likes_count": post._likes,
            "is_liked": post._is_liked,
            "comments_count": post._comments,
            "saves_count": post._saves,
            "is_saved": post._is_saved,
        })

    # next_offset advances by the slice the DB returned (`len(posts)`), not
    # the serialized length (`len(data)`). If the defensive `continue` above
    # skipped a row, basing the next offset on `len(data)` would re-fetch
    # that broken row on the next page; using `len(posts)` walks past it.
    return Response({
        "results": data,
        "has_more": has_more,
        "next_offset": offset + len(posts),
    })
