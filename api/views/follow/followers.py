"""Your followers list: `list_my_followers` (paginated) and
`remove_my_follower`."""


from django.contrib.auth.models import User
from django.db.models import Exists, OuterRef, Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Follow, FollowRequest
from ...services.pagination import decode_cursor, encode_cursor


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_my_followers(request):
    """
    Users following a target user, newest first. Backs both the Profile
    (own) and UserProfile (someone else's) followers lists.

    By default this lists the *current* user's followers. Pass `user_id` to
    list another user's followers instead — gated by the same visibility rule
    as their profile: allowed only when it's your own account, the account is
    public, or you already follow them. Otherwise 403.

    Keyset/cursor pagination ordered by (-created_at, -id). The endpoint used
    to materialise every follower in one response, which is unbounded for a
    popular account. Keyset is preferred over OFFSET here specifically because
    this screen *mutates* the list (the "Remove" action deletes Follow rows):
    OFFSET would skip a follower whenever an earlier row is removed mid-scroll,
    whereas a keyset window is unaffected by inserts/deletes outside it.

    GET params:
      user_id — optional; whose followers to list (default: current user)
      limit   — page size (default 20, capped at 50)
      cursor  — opaque token from the previous page's `next_cursor`

    Response: { "results": [...], "has_more": bool, "next_cursor": str|null }
    """
    # Resolve the target user. Absent (or own id) → the requester, which
    # preserves the original Profile-screen behaviour exactly.
    user_id = request.query_params.get("user_id")
    if user_id:
        # One query fetches the target, their privacy flag (select_related),
        # and — for the gate below — whether the viewer already follows them
        # (folded in as an Exists so the gate costs no separate query).
        target_user = get_object_or_404(
            User.objects
            .select_related("userprofile")
            .annotate(
                viewer_follows=Exists(
                    Follow.objects.filter(
                        follower=request.user, following_id=OuterRef("id")
                    )
                )
            ),
            id=user_id,
        )
    else:
        target_user = request.user

    # Viewing someone else's followers? That's the only case we need the
    # privacy gate AND the per-row follow-button state below.
    is_other_user = target_user.id != request.user.id

    # Privacy gate: a private account's followers are visible only to the
    # owner or to accounts that already follow them — mirrors the post
    # visibility rule (can_view_posts) on the profile endpoint.
    if is_other_user:
        profile = getattr(target_user, "userprofile", None)
        if profile and profile.is_private and not target_user.viewer_follows:
            return Response(
                {"error": "This account is private."},
                status=status.HTTP_403_FORBIDDEN,
            )

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    qs = (
        Follow.objects
        .filter(following=target_user)
        .select_related("follower", "follower__userprofile")
        .order_by("-created_at", "-id")
    )

    # When browsing someone else's followers, fold the viewer's follow-state
    # toward each follower into this same query as Exists annotations (the
    # pattern the feed/page endpoints use) — so the whole page, follow-state
    # included, costs a single SQL round-trip. The own-followers list shows
    # Remove (not Follow), so it skips these and stays annotation-free.
    if is_other_user:
        qs = qs.annotate(
            viewer_is_following=Exists(
                Follow.objects.filter(
                    follower=request.user, following_id=OuterRef("follower_id")
                )
            ),
            viewer_has_requested=Exists(
                FollowRequest.objects.filter(
                    requester=request.user, target_id=OuterRef("follower_id")
                )
            ),
        )

    # Keyset: rows strictly older than the cursor. Compound (created_at, id)
    # comparison keeps ordering total and stable even when two follows share a
    # timestamp, so no row is skipped or repeated across pages.
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
    for f in follows:
        follower = f.follower
        avatar = None
        profile = getattr(follower, "userprofile", None)
        if profile and profile.avatar:
            avatar = request.build_absolute_uri(profile.avatar.url)

        row = {
            "id": follower.id,
            "username": follower.username,
            "avatar": avatar,
        }
        # Follow-button state relative to the viewer — only when browsing
        # someone else's followers (own list renders Remove instead).
        if is_other_user:
            row["is_self"] = follower.id == request.user.id
            row["is_following"] = f.viewer_is_following
            row["has_requested_follow"] = f.viewer_has_requested
            row["is_private"] = profile.is_private if profile else False
        results.append(row)

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
def remove_my_follower(request):
    """
    Severs the Follow relationship where the given user follows the
    current user. The frontend keeps the row visible with a "Removed"
    label until the next visit — this just deletes the DB row.
    """
    user_id = request.data.get("user_id")
    if not user_id:
        return Response({"error": "user_id is required"}, status=400)

    Follow.objects.filter(
        following=request.user,
        follower_id=user_id,
    ).delete()
    return Response({"status": "removed"})
