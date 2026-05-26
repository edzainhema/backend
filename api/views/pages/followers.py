"""Page followers and follow-requests: approve/reject requests, list and remove followers."""


from django.core.cache import cache
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Notification, Page, PageFollow, PageFollowRequest
from ...utils import decode_cursor, encode_cursor, push_to_user

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def approve_page_follow_request(request):
    """
    Page owner approves a pending follow request.
    POST /pages/follow/approve/  { request_id }
    """
    request_id = request.data.get("request_id")
    if not request_id:
        return Response({"error": "request_id required"}, status=400)

    try:
        pfr = PageFollowRequest.objects.select_related(
            "requester", "page", "page__owner"
        ).get(id=request_id)
    except PageFollowRequest.DoesNotExist:
        return Response({"error": "Request not found"}, status=404)

    if pfr.page.owner != request.user:
        return Response({"error": "Not allowed"}, status=403)

    requester = pfr.requester
    page = pfr.page

    # Create follow (idempotent)
    PageFollow.objects.get_or_create(user=requester, page=page)

    # Invalidate the requester's feed caches — their followed_pages set just
    # changed, so feed_ctx and the suggested feed must be rebuilt.
    cache.delete(f"feed_ctx:{requester.id}")
    cache.delete(f"suggested_feed_scores:{requester.id}")

    # Clean up pending request and any pending page_follow_request notification
    pfr.delete()
    Notification.objects.filter(
        recipient=page.owner,
        actor=requester,
        notification_type="page_follow_request",
        page=page,
    ).delete()

    # Notify requester
    Notification.objects.create(
        recipient=requester,
        actor=request.user,
        notification_type="page_follow_approved",
        page=page,
    )
    push_to_user(
        requester,
        title="Follow request approved",
        body=f"Your request to follow {page.name} was approved",
        extra_data={"type": "page_follow_approved", "page_id": page.id},
    )

    return Response({"status": "approved"})



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reject_page_follow_request(request):
    """
    Page owner rejects a pending follow request.
    POST /pages/follow/reject/  { request_id }
    """
    request_id = request.data.get("request_id")
    if not request_id:
        return Response({"error": "request_id required"}, status=400)

    try:
        pfr = PageFollowRequest.objects.select_related(
            "page", "page__owner"
        ).get(id=request_id)
    except PageFollowRequest.DoesNotExist:
        return Response({"error": "Request not found"}, status=404)

    if pfr.page.owner != request.user:
        return Response({"error": "Not allowed"}, status=403)

    # Clean up request and its notification
    Notification.objects.filter(
        recipient=pfr.page.owner,
        actor=pfr.requester,
        notification_type="page_follow_request",
        page=pfr.page,
    ).delete()
    pfr.delete()

    return Response({"status": "rejected"})



@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_page_followers(request):
    """
    Followers of a page, newest first. Keyset/cursor pagination ordered by
    (-created_at, -id).

    The endpoint used to materialise every follower in one response, which is
    unbounded for a popular page — the exact place this matters. Matches the
    contract already used by this page's own posts (get_page_detail) and the
    page directory (list_pages).

    GET params:
      page_id — required
      limit   — page size (default 20, capped at 50)
      cursor  — opaque token from the previous page's `next_cursor`

    Response: { "results": [...], "has_more": bool, "next_cursor": str|null }
    """
    page_id = request.query_params.get("page_id")
    page = get_object_or_404(Page, id=page_id)

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    qs = (
        PageFollow.objects
        .filter(page=page)
        .select_related("user", "user__userprofile")
        .order_by("-created_at", "-id")
    )

    # Keyset: rows strictly older than the cursor. Compound (created_at, id)
    # comparison keeps ordering total/stable even when follows share a
    # timestamp, so no follower is skipped or repeated across pages.
    cursor = decode_cursor(request.query_params.get("cursor"))
    last_created = parse_datetime(cursor["created_at"]) if cursor.get("created_at") else None
    last_id = cursor.get("id")
    if last_created is not None and last_id is not None:
        qs = qs.filter(
            Q(created_at__lt=last_created)
            | Q(created_at=last_created, id__lt=last_id)
        )

    # Fetch one extra row to detect `has_more` without a second COUNT query.
    follows = list(qs[: limit + 1])
    has_more = len(follows) > limit
    follows = follows[:limit]

    results = []
    for pf in follows:
        avatar = None
        profile = getattr(pf.user, "userprofile", None)
        if profile and profile.avatar:
            avatar = request.build_absolute_uri(profile.avatar.url)

        results.append({
            "id": pf.user.id,
            "username": pf.user.username,
            "avatar": avatar,
        })

    next_cursor = None
    if has_more and follows:
        last = follows[-1]
        next_cursor = encode_cursor({
            "created_at": last.created_at.isoformat(),
            "id": last.id,
        })

    return Response({
        "results": results,
        "has_more": has_more,
        "next_cursor": next_cursor,
    })



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def remove_page_follower(request):
    """
    Owner-only: kick a follower off the page. The frontend keeps the row
    visible until the next visit so the user gets a "Removed" confirmation
    chip — this just severs the underlying PageFollow.
    """
    page_id = request.data.get("page_id")
    user_id = request.data.get("user_id")

    if not page_id or not user_id:
        return Response(
            {"error": "page_id and user_id are required"},
            status=400,
        )

    page = get_object_or_404(Page, id=page_id)

    if page.owner_id != request.user.id:
        return Response(
            {"error": "Only the page owner can remove followers."},
            status=403,
        )

    PageFollow.objects.filter(page=page, user_id=user_id).delete()
    return Response({"status": "removed"})
