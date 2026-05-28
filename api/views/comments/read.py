"""Reading a post's comment thread (`get_comments`)."""


from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Comment, CommentLike, Post
from ...serializers import CommentSerializer
from ...services.feed_helpers import viewer_can_see_post

COMMENTS_PAGE_DEFAULT = 20
COMMENTS_PAGE_MAX = 50


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_comments(request):
    post_id = request.query_params.get("post_id")

    if not post_id:
        return Response(
            {"error": "post_id required"},
            status=400
        )

    # Pagination: cap top-level comments per request so a viral post can't
    # force the endpoint to serialize thousands of rows + their replies in
    # one response. Clients page in by passing ?offset=N&limit=M; clients
    # that omit these get the default page (preserves backwards-compatible
    # behaviour for any callers that haven't been updated yet, just with a
    # bounded payload).
    try:
        limit = int(request.query_params.get("limit", COMMENTS_PAGE_DEFAULT))
    except (TypeError, ValueError):
        limit = COMMENTS_PAGE_DEFAULT
    limit = max(1, min(limit, COMMENTS_PAGE_MAX))

    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    post = get_object_or_404(
        Post.objects.select_related("user", "user__userprofile", "page"),
        id=post_id,
    )

    # Visibility check: previously this endpoint only filtered comments by
    # block status, but happily returned the comment list for a post the
    # viewer couldn't otherwise see (private account they don't follow,
    # private page they don't follow, etc.). Return 404 — not 403 — so we
    # don't leak the existence of the post to a viewer who shouldn't know
    # about it.
    if not viewer_can_see_post(request.user, post):
        return Response({"error": "Not found"}, status=404)

    blocked_pairs = BlockedUser.objects.involving(request.user).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_user_ids = set()
    for u, b in blocked_pairs:
        blocked_user_ids.add(u)
        blocked_user_ids.add(b)

    blocked_user_ids.discard(request.user.id)

    # Annotate likes_count + is_liked at the SQL layer so the database returns
    # an integer and a boolean per comment, rather than handing back every
    # CommentLike row for Python to len()/scan. This is what keeps the endpoint
    # cheap when a single comment gets thousands of likes — the wire and memory
    # cost is now O(1) per comment instead of O(likes).
    #
    # The likes_count_ann uses a filtered Count so likes from users the viewer
    # has blocked (or who have blocked the viewer) don't inflate the visible
    # count. Otherwise the displayed like number could disagree with the set
    # of comments the viewer can actually see.
    viewer_liked = CommentLike.objects.filter(
        comment=OuterRef("pk"),
        user=request.user,
    )

    likes_count_expr = Count(
        "likes",
        filter=~Q(likes__user_id__in=blocked_user_ids),
    )

    # Build the reply queryset once, with the same block-filter, ordering, and
    # annotations the top-level comments use. Attaching it via Prefetch
    # (to_attr="filtered_replies") keeps the cache intact — calling
    # c.replies.exclude(...) on a related manager would otherwise bust the
    # prefetch and issue a fresh query per top-level comment.
    reply_qs = (
        Comment.objects
        .exclude(user_id__in=blocked_user_ids)
        .select_related("user", "user__userprofile")
        .annotate(
            likes_count_ann=likes_count_expr,
            is_liked_ann=Exists(viewer_liked),
        )
        .order_by("created_at")
    )

    base = (
        Comment.objects
        .filter(
            post=post,
            parent__isnull=True
        )
        .exclude(user_id__in=blocked_user_ids)
        .select_related("user", "user__userprofile")
        .annotate(
            likes_count_ann=likes_count_expr,
            is_liked_ann=Exists(viewer_liked),
        )
        .prefetch_related(
            Prefetch("replies", queryset=reply_qs, to_attr="filtered_replies"),
        )
        .order_by("created_at")
    )

    # Fetch one extra row to learn whether another page exists without
    # paying for a separate COUNT(*) (which would scan the whole table for
    # popular posts). If we got back limit+1 rows, more pages remain; drop
    # the extra before serializing.
    page = list(base[offset:offset + limit + 1])
    has_more = len(page) > limit
    page = page[:limit]

    ctx = {'request': request, 'viewer': request.user}
    data = []

    for c in page:
        # filtered_replies is the cached, pre-filtered list populated by the
        # Prefetch above — zero extra queries per top-level comment.
        replies = c.filtered_replies
        comment_data = CommentSerializer(c, context=ctx).data
        comment_data['replies'] = CommentSerializer(
            replies, many=True, context=ctx
        ).data
        data.append(comment_data)

    return Response({
        "comments": data,
        "has_more": has_more,
        "next_offset": offset + len(data) if has_more else None,
    })


