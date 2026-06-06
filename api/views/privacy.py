

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import transaction
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
from ..services.notification_cache import invalidate_unread_count_cache
from ..services.pagination import decode_cursor, encode_cursor
from ..services.feed_helpers import (
    get_social_sets,
)


def _invalidate_viewer_feed_cache(user_id):
	"""Drop the per-viewer feed caches so a mute/unmute (or block/unblock)
	takes effect on the next feed load instead of lingering for the cache TTL.

	Clears `feed_ctx:{id}` — the per-viewer exclusion sets (blocked/muted ids)
	that every feed surface reads, TTL ~90s — and `suggested_feed_scores:{id}`,
	the cached suggested-feed ranking.

	The discovery RAILS keep their own per-viewer score keys
	(`feed:activity_scores:*`, `feed:friend_network:*`, `feed:nearby:*`,
	`feed:collaborative:*`), but they do NOT need clearing here: since audit H1
	each rail re-derives its block/mute/visibility exclusions from `feed_ctx`
	on every cache-hit (see scoring.rehydrate_visible_slice), so clearing
	`feed_ctx` is what makes them re-filter. A mute therefore hides the author
	immediately. The one residual is that an *unmuted* author's posts reappear
	in the discovery rails only within the rail TTL — re-filtering can drop a
	cached post but can't add one back, and the rail's score list was built
	without them — which is acceptable latency for discovery (the author
	reappears in the followed/suggested feed at once via the keys cleared here),
	not a leak.

	Intentionally does NOT touch the notification unread-count cache: notification
	listings filter on BLOCKS, not mutes, so only the block path invalidates that
	(see toggle_block_user). Muting must not.
	"""
	cache.delete(f"feed_ctx:{user_id}")
	cache.delete(f"suggested_feed_scores:{user_id}")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_mute_user(request):
	target_id = request.data.get("user_id")
	target = get_object_or_404(User, id=target_id)
	# L4: you can't mute yourself. Without this the self-referential row
	# persists and you show up in your own muted list (the feed context
	# defensively discards self-ids so nothing crashes, but the row is junk).
	# Mirror the report views, which 400 on self-target.
	if target == request.user:
		return Response({"error": "You cannot mute yourself"}, status=400)
	obj = MutedUser.objects.filter(
		user=request.user,
		muted_user=target
	)
	if obj.exists():
		obj.delete()
		# M1: an unmute must invalidate the viewer's feed caches too —
		# symmetric with the mute branch below, and with toggle_block_user
		# which already invalidates on both block AND unblock. Without this the
		# just-unmuted author stays filtered out of the feed for up to the
		# feed_ctx TTL (~90s), so the unmute appears not to work.
		_invalidate_viewer_feed_cache(request.user.id)
		return Response({"status": "unmuted"})
	# L2: get_or_create, not create() — a double-tap that both pass the
	# exists() check above would otherwise have the second insert 500 on the
	# unique_together('user','muted_user') constraint. get_or_create makes the
	# concurrent mute idempotent ("muted" either way).
	MutedUser.objects.get_or_create(
		user=request.user,
		muted_user=target
	)
	# Invalidate feed context so muted user is excluded from next feed load
	_invalidate_viewer_feed_cache(request.user.id)
	return Response({"status": "muted"})

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_block_user(request):
    target_id = request.data.get("user_id")
    target = get_object_or_404(User, id=target_id)

    # L4: you can't block yourself — a self-referential block row is junk (the
    # feed context defensively discards self-ids, so it never breaks visibility,
    # but it pollutes your own blocked list). Mirror the report views' 400.
    if target == request.user:
        return Response({"error": "You cannot block yourself"}, status=400)

    obj = BlockedUser.objects.filter(
        user=request.user,
        blocked_user=target
    )

    if obj.exists():
        obj.delete()
        _invalidate_viewer_feed_cache(request.user.id)
        _invalidate_viewer_feed_cache(target.id)
        # M8: unblock restores the OTHER party's notifications to BOTH
        # parties' unread counts (list_notifications + unread count
        # both filter on blocked-actor ids in either direction). Without
        # this invalidation the bell badge stays wrong for up to
        # UNREAD_COUNT_CACHE_TTL_S (30s).
        invalidate_unread_count_cache(request.user.id)
        invalidate_unread_count_cache(target.id)
        return Response({"status": "unblocked"})

    # L3: block is an all-or-nothing operation — tear down the two users'
    # relationships AND write the block row, or do neither. Without the atomic
    # wrapper, a failure between the deletes and the create()/get_or_create
    # left the relationships gone but no block row written — a "half-blocked"
    # limbo (follows dropped, yet the block that justified dropping them never
    # landed). One transaction makes a mid-way failure roll the deletes back.
    with transaction.atomic():
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

        # L2: get_or_create, not create() — two concurrent block requests both
        # pass the exists() check at the top and reach here; the second create()
        # would 500 on the unique_together('user','blocked_user') constraint.
        # get_or_create makes the concurrent block idempotent ("blocked" either
        # way). The teardown above is all idempotent deletes, so running it on
        # the losing request is harmless.
        BlockedUser.objects.get_or_create(
            user=request.user,
            blocked_user=target
        )

    # Cache invalidation runs AFTER the transaction commits — invalidating from
    # inside the atomic block would let a concurrent read repopulate the cache
    # from the not-yet-committed (or about-to-roll-back) state.
    # Invalidate both users' feed contexts and suggested-feed scores.
    _invalidate_viewer_feed_cache(request.user.id)
    _invalidate_viewer_feed_cache(target.id)

    # M8: a fresh block drops the OTHER party's notifications from
    # BOTH parties' unread counts (list_notifications and
    # unread_notifications_count both filter on blocked-actor ids in
    # either direction). Without this invalidation the bell badge
    # stays wrong for up to UNREAD_COUNT_CACHE_TTL_S (30s) -- the
    # cached count still includes notifications that the list view
    # has already started excluding.
    invalidate_unread_count_cache(request.user.id)
    invalidate_unread_count_cache(target.id)

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
