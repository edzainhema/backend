

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db.models import (
    Case, IntegerField, Q, Value, When,
)
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response



from ..models import (
    BlockedUser, Follow, FollowRequest, MutedUser, PageFollow, PageFollowRequest,
)
from ..utils import (
    decode_cursor,
    encode_cursor,
)
from ..services.feed_helpers import (
    get_social_sets,
)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_mute_user(request):
	target_id = request.data.get("user_id")
	target = get_object_or_404(User, id=target_id)
	obj = MutedUser.objects.filter(
		user=request.user,
		muted_user=target
	)
	if obj.exists():
		obj.delete()
		return Response({"status": "unmuted"})
	MutedUser.objects.create(
		user=request.user,
		muted_user=target
	)
	# Invalidate feed context so muted user is excluded from next feed load
	cache.delete(f"feed_ctx:{request.user.id}")
	cache.delete(f"suggested_feed_scores:{request.user.id}")
	return Response({"status": "muted"})

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_block_user(request):
    target_id = request.data.get("user_id")
    target = get_object_or_404(User, id=target_id)

    obj = BlockedUser.objects.filter(
        user=request.user,
        blocked_user=target
    )

    if obj.exists():
        obj.delete()
        cache.delete(f"feed_ctx:{request.user.id}")
        cache.delete(f"feed_ctx:{target.id}")
        cache.delete(f"suggested_feed_scores:{request.user.id}")
        cache.delete(f"suggested_feed_scores:{target.id}")
        return Response({"status": "unblocked"})

    # 🔥 Remove follows both ways
    Follow.objects.filter(
        Q(follower=request.user, following=target) |
        Q(follower=target, following=request.user)
    ).delete()

    # 🔥 Remove pending follow requests both ways. Without this the blocked
    # party keeps showing up in the blocker's pending-request list (and the
    # blocker keeps showing up in the target's) even though re-issuing the
    # follow would now be refused by toggle_follow's block check.
    FollowRequest.objects.filter(
        Q(requester=request.user, target=target) |
        Q(requester=target, target=request.user)
    ).delete()

    # 🔥 Remove page-follow rows in both directions:
    #   * blocker's follows on pages the target owns
    #   * target's follows on pages the blocker owns
    # Same reasoning as the user follow above — the relationship lives
    # entirely within these two users' page graphs.
    PageFollow.objects.filter(
        Q(user=request.user, page__owner=target) |
        Q(user=target, page__owner=request.user)
    ).delete()

    # 🔥 Remove pending page-follow requests in both directions so neither
    # user keeps a stale pending row pointing at the other's page.
    PageFollowRequest.objects.filter(
        Q(requester=request.user, page__owner=target) |
        Q(requester=target, page__owner=request.user)
    ).delete()

    # 🔥 Remove mute if exists
    MutedUser.objects.filter(
        user=request.user,
        muted_user=target
    ).delete()

    BlockedUser.objects.create(
        user=request.user,
        blocked_user=target
    )

    # Invalidate both users' feed contexts and suggested-feed scores.
    cache.delete(f"feed_ctx:{request.user.id}")
    cache.delete(f"feed_ctx:{target.id}")
    cache.delete(f"suggested_feed_scores:{request.user.id}")
    cache.delete(f"suggested_feed_scores:{target.id}")

    return Response({"status": "blocked"})

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_blocked_users(request):
    """
    Users the caller has blocked, most-recently-blocked first. Keyset/cursor
    pagination ordered by (-created_at, -id).

    The endpoint used to return every block in a single response; this paginates
    it on the same contract as the rest of the people lists.

    GET params:
      limit  — page size (default 20, capped at 50)
      cursor — opaque token from the previous page's `next_cursor`

    Response: { "results": [...], "has_more": bool, "next_cursor": str|null }
    """
    user = request.user

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    qs = (
        BlockedUser.objects
        .filter(user=user)
        .select_related("blocked_user", "blocked_user__userprofile")
        .order_by("-created_at", "-id")
    )

    # Keyset: rows strictly older than the cursor. Compound comparison keeps
    # ordering total/stable when two blocks share a timestamp.
    cursor = decode_cursor(request.query_params.get("cursor"))
    last_created = parse_datetime(cursor["created_at"]) if cursor.get("created_at") else None
    last_id = cursor.get("id")
    if last_created is not None and last_id is not None:
        qs = qs.filter(
            Q(created_at__lt=last_created)
            | Q(created_at=last_created, id__lt=last_id)
        )

    # Fetch one extra row to detect `has_more` without a second COUNT query.
    blocks = list(qs[: limit + 1])
    has_more = len(blocks) > limit
    blocks = blocks[:limit]

    results = []
    for b in blocks:
        blocked_user = b.blocked_user
        up = getattr(blocked_user, "userprofile", None)

        results.append({
            "id": blocked_user.id,
            "username": blocked_user.username,
            "avatar": (
                request.build_absolute_uri(up.avatar.url)
                if up and up.avatar
                else None
            ),
            "is_blocked": True,
        })

    next_cursor = None
    if has_more and blocks:
        last = blocks[-1]
        next_cursor = encode_cursor({
            "created_at": last.created_at.isoformat(),
            "id": last.id,
        })

    return Response({
        "results": results,
        "has_more": has_more,
        "next_cursor": next_cursor,
    })

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_muted_users(request):
    """
    Users the caller has muted, most-recently-muted first. Keyset/cursor
    pagination ordered by (-created_at, -id).

    The endpoint used to return every mute in a single response; this paginates
    it on the same contract as list_blocked_users and the rest of the people
    lists.

    GET params:
      limit  — page size (default 20, capped at 50)
      cursor — opaque token from the previous page's `next_cursor`

    Response: { "results": [...], "has_more": bool, "next_cursor": str|null }
    """
    user = request.user

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    qs = (
        MutedUser.objects
        .filter(user=user)
        .select_related("muted_user", "muted_user__userprofile")
        .order_by("-created_at", "-id")
    )

    # Keyset: rows strictly older than the cursor. Compound comparison keeps
    # ordering total/stable when two mute rows share a timestamp.
    cursor = decode_cursor(request.query_params.get("cursor"))
    last_created = parse_datetime(cursor["created_at"]) if cursor.get("created_at") else None
    last_id = cursor.get("id")
    if last_created is not None and last_id is not None:
        qs = qs.filter(
            Q(created_at__lt=last_created)
            | Q(created_at=last_created, id__lt=last_id)
        )

    # Fetch one extra row to detect `has_more` without a second COUNT query.
    mutes = list(qs[: limit + 1])
    has_more = len(mutes) > limit
    mutes = mutes[:limit]

    results = []
    for m in mutes:
        muted_user = m.muted_user
        up = getattr(muted_user, "userprofile", None)

        results.append({
            "id": muted_user.id,
            "username": muted_user.username,
            "avatar": (
                request.build_absolute_uri(up.avatar.url)
                if up and up.avatar
                else None
            ),
            "is_muted": True,
        })

    next_cursor = None
    if has_more and mutes:
        last = mutes[-1]
        next_cursor = encode_cursor({
            "created_at": last.created_at.isoformat(),
            "id": last.id,
        })

    return Response({
        "results": results,
        "has_more": has_more,
        "next_cursor": next_cursor,
    })

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def search_blocked_users(request):
    user = request.user
    q = request.query_params.get("q", "").strip()

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    social = get_social_sets(user)

    blocked_ids = set(
        BlockedUser.objects.filter(
            user=user
        ).values_list("blocked_user_id", flat=True)
    )

    # Rank in the DB instead of pulling every username match into memory to
    # sort + slice in Python: blocked (0) > friends (1) > following-only (2) >
    # followers-only (3) > everyone else (4), then alphabetical, id tiebreak.
    users = (
        User.objects
        .filter(username__icontains=q)
        .exclude(id=user.id)
        .select_related("userprofile")
        .annotate(
            social_rank=Case(
                When(id__in=blocked_ids, then=Value(0)),
                When(id__in=social["friends"], then=Value(1)),
                When(id__in=social["following_only"], then=Value(2)),
                When(id__in=social["followers_only"], then=Value(3)),
                default=Value(4),
                output_field=IntegerField(),
            )
        )
        .order_by("social_rank", "username", "id")
    )

    # Offset window: fetch one extra row to detect `has_more` without a COUNT.
    window = list(users[offset : offset + limit + 1])
    has_more = len(window) > limit
    window = window[:limit]

    data = []
    for u in window:
        up = getattr(u, "userprofile", None)

        data.append({
            "id": u.id,
            "username": u.username,
            "avatar": (
                request.build_absolute_uri(up.avatar.url)
                if up and up.avatar
                else None
            ),
            "is_blocked": u.id in blocked_ids,
        })

    return Response({
        "results": data,
        "has_more": has_more,
        "next_offset": (offset + limit) if has_more else None,
    })

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def search_muted_users(request):
    user = request.user
    q = request.query_params.get("q", "").strip()

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    social = get_social_sets(user)

    muted_ids = set(
        MutedUser.objects.filter(
            user=user
        ).values_list("muted_user_id", flat=True)
    )

    # Rank in the DB instead of pulling every username match into memory to
    # sort + slice in Python: muted (0) > friends (1) > following-only (2) >
    # followers-only (3) > everyone else (4), then alphabetical, id tiebreak.
    users = (
        User.objects
        .filter(username__icontains=q)
        .exclude(id=user.id)
        .select_related("userprofile")
        .annotate(
            social_rank=Case(
                When(id__in=muted_ids, then=Value(0)),
                When(id__in=social["friends"], then=Value(1)),
                When(id__in=social["following_only"], then=Value(2)),
                When(id__in=social["followers_only"], then=Value(3)),
                default=Value(4),
                output_field=IntegerField(),
            )
        )
        .order_by("social_rank", "username", "id")
    )

    # Offset window: fetch one extra row to detect `has_more` without a COUNT.
    window = list(users[offset : offset + limit + 1])
    has_more = len(window) > limit
    window = window[:limit]

    data = []
    for u in window:
        up = getattr(u, "userprofile", None)

        data.append({
            "id": u.id,
            "username": u.username,
            "avatar": (
                request.build_absolute_uri(up.avatar.url)
                if up and up.avatar
                else None
            ),
            "is_muted": u.id in muted_ids,
        })

    return Response({
        "results": data,
        "has_more": has_more,
        "next_offset": (offset + limit) if has_more else None,
    })
