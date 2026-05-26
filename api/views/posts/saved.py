"""The viewer's saved-posts feed."""


from django.db.models import Exists, OuterRef, Prefetch, Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, PostLike, PostMedia, SavedPost
from ...services.feed_helpers import (
    likes_count_subquery, comments_count_subquery,
)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saved_posts(request):
    user = request.user

    # --------------------------------------------------
    # 📄 PAGINATION (offset/limit, capped — matches the rest of the codebase
    # such as views/profile.py:108-117). The endpoint was previously
    # unpaginated, which meant a heavy saver could trigger hundreds of
    # follow-up queries and ship the entire collection in a single response.
    # --------------------------------------------------
    try:
        limit = int(request.query_params.get("limit", 30))
    except (TypeError, ValueError):
        limit = 30
    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 60))
    offset = max(0, offset)

    # --------------------------------------------------
    # 🚫 BLOCKED USERS (both directions)
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
    # 🔖 FETCH SAVED POSTS (EXCLUDING BLOCKED)
    # --------------------------------------------------
    # Annotate likes_count / comments_count and viewer_liked at the SQL layer
    # so we don't fire .count() and .exists() inside the per-row loop. Order
    # the media prefetch via `to_attr="ordered_media"` so the per-post media
    # list is sorted by SQL and the prefetch cache survives — calling
    # `post.media.all().order_by("order")` would otherwise re-query per post.
    viewer_liked_subq = PostLike.objects.filter(
        post=OuterRef("post"),
        user=user,
    )

    saved = (
        SavedPost.objects
        .filter(user=user)
        .exclude(post__user_id__in=blocked_user_ids)
        .select_related(
            "post",
            "post__user",
            "post__user__userprofile",
            "post__page",
        )
        .prefetch_related(
            Prefetch(
                "post__media",
                queryset=PostMedia.objects.order_by("order"),
                to_attr="ordered_media",
            ),
        )
        .annotate(
            likes_count_ann=likes_count_subquery(outer="post_id"),
            comments_count_ann=comments_count_subquery(outer="post_id"),
            viewer_liked=Exists(viewer_liked_subq),
        )
        .order_by("-created_at")
    )[offset:offset + limit]

    # --------------------------------------------------
    # 🧾 SERIALIZE
    # --------------------------------------------------
    data = []

    for s in saved:
        post = s.post
        up = getattr(post.user, "userprofile", None)

        data.append({
            "id": post.id,
            "description": post.description,
            "created_at": post.created_at,

            # 👤 POST OWNER
            "user": {
                "id": post.user.id,
                "username": post.user.username,
                "avatar": (
                    request.build_absolute_uri(up.avatar.url)
                    if up and up.avatar
                    else None
                ),
            },

            # 📄 PAGE (optional)
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

            # ❤️ LIKES — read SQL annotations
            "likes_count": s.likes_count_ann,
            "is_liked": bool(s.viewer_liked),

            # 💬 COMMENTS
            "comments_count": s.comments_count_ann,

            # 🔖 SAVES (always True on this endpoint)
            "is_saved": True,

            # 🖼️ MEDIA — populated by the Prefetch above
            "media": [
                {
                    "id": m.id,
                    "file": request.build_absolute_uri(m.file.url),
                    "order": m.order,
                    "width": m.width,
                    "height": m.height,
                }
                for m in getattr(post, "ordered_media", [])
            ],
        })

    return Response(data)
