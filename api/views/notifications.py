

from django.core.cache import cache
from django.db.models import (
    Exists, OuterRef, Prefetch,
)

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response



from ..models import (
    BlockedUser, FollowRequest, Notification, PageFollowRequest, PageInvite, Post,
    PostLike, PostMedia, SavedPost,
)
from ..serializers import (
    NotificationSerializer,
)
from ..services.feed_helpers import (
    likes_count_subquery, comments_count_subquery, saves_count_subquery,
)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_notifications(request):
    user = request.user

    # --------------------------------------------------
    # PAGINATION
    # The endpoint used to return every notification the user had ever
    # received in one response. For a long-tenured account that grew to
    # tens of thousands of rows -- a single tap on the bell could OOM the
    # response. Mirrors the offset/limit pattern used in reels_feed and
    # get_page_detail (same limit+1 trick for has_more).
    # --------------------------------------------------
    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 50))
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
    # FETCH NOTIFICATIONS (EXCLUDING BLOCKED ACTORS)
    #
    # The post-media prefetches now use Prefetch(..., to_attr='ordered_media')
    # so the serializer can read a pre-sorted list directly. Previously the
    # serializer called `post.media.order_by('order').first()`, which bypasses
    # the prefetch cache and fires one extra query per notification that
    # references a post -- a sneaky N+1 hiding under what looked like a
    # properly prefetched queryset.
    # --------------------------------------------------
    base_qs = (
        Notification.objects
        .filter(recipient=user)
        .exclude(actor_id__in=blocked_user_ids)
        .select_related(
            "actor",
            "actor__userprofile",
            "comment__post",
            "comment__post__user__userprofile",
            "comment__post__page",
            "media",
            "media__user__userprofile",
            "media__page",
            "page",
        )
        .prefetch_related(
            Prefetch(
                "comment__post__media",
                queryset=PostMedia.objects.order_by("order"),
                to_attr="ordered_media",
            ),
            Prefetch(
                "media__media",
                queryset=PostMedia.objects.order_by("order"),
                to_attr="ordered_media",
            ),
        )
        .order_by("-created_at")
    )

    # limit+1 trick -- fetch one extra row so we can tell the client whether
    # more pages exist without firing a separate COUNT(*) on the table.
    fetched = list(base_qs[offset:offset + limit + 1])
    has_more = len(fetched) > limit
    notif_list = fetched[:limit]

    # --------------------------------------------------
    # ANNOTATE COUNTS + LIKED/SAVED ON REFERENCED POSTS (avoids N+1 in get_post())
    # Now operates on the paginated slice rather than every notification ever.
    # --------------------------------------------------
    post_ids = set()
    for n in notif_list:
        if n.media_id:
            post_ids.add(n.media_id)
        elif n.comment_id and hasattr(n, 'comment') and n.comment and n.comment.post_id:
            post_ids.add(n.comment.post_id)

    if post_ids:
        annotated_posts = {
            p.id: p
            for p in Post.objects.filter(id__in=post_ids).annotate(
                likes_count_ann=likes_count_subquery(),
                comments_count_ann=comments_count_subquery(),
                saves_count_ann=saves_count_subquery(),
                is_liked_ann=Exists(
                    PostLike.objects.filter(post_id=OuterRef('pk'), user_id=user.id)
                ),
                is_saved_ann=Exists(
                    SavedPost.objects.filter(post_id=OuterRef('pk'), user_id=user.id)
                ),
            ).prefetch_related(
                Prefetch(
                    "media",
                    queryset=PostMedia.objects.order_by("order"),
                    to_attr="ordered_media",
                ),
            )
        }
        # Attach the annotated (and ordered-media-prefetched) post back onto
        # each notification so the serializer reads it instead of the
        # unannotated copy that came through the Notification prefetch path.
        for n in notif_list:
            if n.media_id and n.media_id in annotated_posts:
                n.media = annotated_posts[n.media_id]
            elif (n.comment_id and hasattr(n, 'comment') and n.comment
                  and n.comment.post_id in annotated_posts):
                n.comment.post = annotated_posts[n.comment.post_id]

    # --------------------------------------------------
    # BULK LOOKUP MAPS (avoids N+1 per notification type)
    # --------------------------------------------------
    # follow_request_map: (actor_id, recipient_id) -> FollowRequest.id
    fr_map = {
        (fr.requester_id, fr.target_id): fr.id
        for fr in FollowRequest.objects.filter(target=user)
    }

    # page_invite_map: (actor_id, recipient_id) -> PageInvite.id
    # Also build page_invite_page_map so get_invited_page avoids extra queries.
    page_invites = list(
        PageInvite.objects.filter(invited_user=user).select_related("page")
    )
    pi_map      = {(pi.invited_by_id, pi.invited_user_id): pi.id   for pi in page_invites}
    pi_page_map = {(pi.invited_by_id, pi.invited_user_id): pi.page for pi in page_invites}

    # page_follow_request_map: (actor_id, page_id) -> PageFollowRequest.id
    # Covers all pages owned by this user (for page_follow_request notifications).
    pfr_map = {
        (pfr.requester_id, pfr.page_id): pfr.id
        for pfr in PageFollowRequest.objects.filter(page__owner=user)
    }

    # --------------------------------------------------
    # SERIALIZE
    # --------------------------------------------------
    context = {
        "request":                 request,
        "follow_request_map":      fr_map,
        "page_invite_map":         pi_map,
        "page_invite_page_map":    pi_page_map,
        "page_follow_request_map": pfr_map,
    }
    # Pass `notif_list` (the materialised slice) rather than the queryset
    # so the serializer sees the annotated/replaced post objects we attached
    # above. Iterating the queryset again would re-evaluate it.
    data = list(NotificationSerializer(notif_list, many=True, context=context).data)

    # post_thumbnail is already included inside each `post` object returned by
    # the serializer's get_post() method. We surface it at the top level here as
    # a convenience alias so the frontend keeps working without changes.
    for i, notif_data in enumerate(data):
        post_data = notif_data.get("post")
        data[i]["post_thumbnail"] = post_data["thumbnail"] if post_data else None

    return Response({
        "results": data,
        "has_more": has_more,
        "next_offset": offset + len(notif_list),
    })


# ---------------- UNREAD-COUNT CACHE ----------------
# Cache helpers live in api.notification_cache (not here) so apps.ready()
# can import them without dragging in views/__init__.py. The 30 s TTL is
# invalidated on every Notification create (signal in apps.py) and on
# every mark-read path, so the dot still updates promptly when it matters.
from ..notification_cache import (
    UNREAD_COUNT_CACHE_TTL_S,
    _unread_count_cache_key,
    invalidate_unread_count_cache,
)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mark_notification_read(request):
    notif_id = request.data.get('id')

    Notification.objects.filter(
        id=notif_id,
        recipient=request.user
    ).update(is_read=True)

    # `.update()` bypasses post_save, so invalidate explicitly.
    invalidate_unread_count_cache(request.user.id)

    return Response({"status": "ok"})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mark_all_notifications_read(request):
    """Mark every unread notification for the current user as read in one query."""
    Notification.objects.filter(
        recipient=request.user,
        is_read=False,
    ).update(is_read=True)

    # `.update()` bypasses post_save, so invalidate explicitly.
    invalidate_unread_count_cache(request.user.id)

    return Response({"status": "ok"})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def unread_notifications_count(request):
    """Lightweight counter for the home-header bell badge.

    Returns the number of unread notifications for the current user, excluding
    notifications whose actor is blocked in either direction (kept consistent
    with list_notifications so the dot and the list agree).

    Cached per user for UNREAD_COUNT_CACHE_TTL_S seconds. The cache is
    invalidated on every Notification create (via signal) and on every
    mark-read path, so the dot still updates promptly when it matters.
    """
    user = request.user

    cache_key = _unread_count_cache_key(user.id)
    cached = cache.get(cache_key)
    if cached is not None:
        return Response({"count": cached})

    blocked_pairs = BlockedUser.objects.involving(user).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_user_ids = set()
    for u, b in blocked_pairs:
        blocked_user_ids.add(u)
        blocked_user_ids.add(b)
    blocked_user_ids.discard(user.id)

    count = (
        Notification.objects
        .filter(recipient=user, is_read=False)
        .exclude(actor_id__in=blocked_user_ids)
        .count()
    )

    cache.set(cache_key, count, timeout=UNREAD_COUNT_CACHE_TTL_S)

    return Response({"count": count})
