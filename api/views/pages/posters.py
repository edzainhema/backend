"""Page posters: list, toggle, and search co-poster permissions."""


from django.contrib.auth.models import User
from django.db.models import Case, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Follow, Notification, Page, PageFollow, PagePoster
from ...utils import decode_cursor, encode_cursor, push_to_user

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_page_posters(request):
    """
    Users allowed to post on a page, most-recently-added first. Only the page
    owner can call this. Keyset/cursor pagination ordered by (-added_at, -id) on
    the same contract as the rest of the people lists.

    GET params:
      page_id — required
      limit   — page size (default 20, capped at 50)
      cursor  — opaque token from the previous page's `next_cursor`

    Response: { "results": [...], "has_more": bool, "next_cursor": str|null }
    """
    page_id = request.query_params.get("page_id")

    page = get_object_or_404(Page, id=page_id)

    if page.owner != request.user:
        return Response({"error": "Not allowed"}, status=403)

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    qs = (
        PagePoster.objects
        .filter(page=page)
        .select_related("user")
        .order_by("-added_at", "-id")
    )

    # Keyset: rows strictly older than the cursor. Compound comparison keeps
    # ordering total/stable when two posters share a timestamp.
    cursor = decode_cursor(request.query_params.get("cursor"))
    last_added = parse_datetime(cursor["added_at"]) if cursor.get("added_at") else None
    last_id = cursor.get("id")
    if last_added is not None and last_id is not None:
        qs = qs.filter(
            Q(added_at__lt=last_added)
            | Q(added_at=last_added, id__lt=last_id)
        )

    # Fetch one extra row to detect `has_more` without a second COUNT query.
    posters = list(qs[: limit + 1])
    has_more = len(posters) > limit
    posters = posters[:limit]

    results = [
        {
            "id": p.user.id,
            "username": p.user.username,
        }
        for p in posters
    ]

    next_cursor = None
    if has_more and posters:
        last = posters[-1]
        next_cursor = encode_cursor({
            "added_at": last.added_at.isoformat(),
            "id": last.id,
        })

    return Response({
        "results": results,
        "has_more": has_more,
        "next_cursor": next_cursor,
    })



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_page_poster(request):
    page_id = request.data.get("page_id")
    user_id = request.data.get("user_id")

    page = get_object_or_404(Page, id=page_id)

    if page.owner != request.user:
        return Response({"error": "Not allowed"}, status=403)

    poster = PagePoster.objects.filter(
        page=page,
        user_id=user_id
    ).first()

    if poster:
        poster.delete()
        return Response({"status": "removed"})
    else:
        target_user = get_object_or_404(User, id=user_id)
        PagePoster.objects.create(
            page=page,
            user=target_user,
        )
        # Notify the user they've been granted poster rights
        Notification.objects.create(
            recipient=target_user,
            actor=request.user,
            notification_type="page_poster_added",
            page=page,
        )
        push_to_user(
            target_user,
            title="Poster access granted",
            body=f"You can now post on {page.name}",
            extra_data={
                "type": "page_poster_added",
                "page_id": page.id,
                "actor_id": request.user.id,
            },
        )
        return Response({"status": "added"})



@api_view(["GET"])
@permission_classes([IsAuthenticated])
def search_page_posters(request):
    owner = request.user
    page_id = request.query_params.get("page_id")
    q = request.query_params.get("q", "").strip()

    if not page_id:
        return Response(
            {"error": "page_id required"},
            status=400
        )

    page = get_object_or_404(Page, id=page_id)

    # --------------------------------------------------
    # 🔐 ONLY PAGE OWNER
    # --------------------------------------------------
    if page.owner != owner:
        return Response({"error": "Not allowed"}, status=403)

    if not q:
        return Response({"results": [], "has_more": False, "next_offset": None})

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

    # --------------------------------------------------
    # 🚫 BLOCKED USERS (BOTH DIRECTIONS)
    # --------------------------------------------------
    blocked_pairs = BlockedUser.objects.involving(owner).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_ids = set()
    for u, b in blocked_pairs:
        blocked_ids.add(u)
        blocked_ids.add(b)

    blocked_ids.discard(owner.id)

    # --------------------------------------------------
    # 👥 RELATIONSHIP SETS
    # --------------------------------------------------
    allowed_posters = set(
        PagePoster.objects.filter(page=page)
        .values_list("user_id", flat=True)
    )

    page_followers = set(
        PageFollow.objects.filter(page=page)
        .values_list("user_id", flat=True)
    )

    owner_following = set(
        Follow.objects.filter(follower=owner)
        .values_list("following_id", flat=True)
    )

    # --------------------------------------------------
    # 🔍 SEARCH USERS (MATCH QUERY)
    # --------------------------------------------------
    # --------------------------------------------------
    # 🧮 RANKING (in the DB)
    # --------------------------------------------------
    # Rank in the query instead of slicing 100 rows and sorting in Python. The
    # old code took the first 100 by default ordering *before* ranking, so a
    # high-rank match outside that window was silently dropped. Allowed poster
    # (0) ranks above page follower (1) above owner-following (2) above everyone
    # else (3), then alphabetical, id as the final tiebreaker for a stable
    # offset window.
    users = (
        User.objects
        .filter(username__icontains=q)
        .exclude(id__in=blocked_ids)
        .exclude(id=owner.id)
        .annotate(
            poster_rank=Case(
                When(id__in=allowed_posters, then=Value(0)),
                When(id__in=page_followers, then=Value(1)),
                When(id__in=owner_following, then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            )
        )
        .order_by("poster_rank", "username", "id")
        .distinct()
    )

    # Offset window: fetch one extra row to detect `has_more` without a COUNT.
    window = list(users[offset : offset + limit + 1])
    has_more = len(window) > limit
    window = window[:limit]

    # --------------------------------------------------
    # 📦 RESPONSE
    # --------------------------------------------------
    return Response({
        "results": [
            {
                "id": u.id,
                "username": u.username,
                "is_allowed": u.id in allowed_posters,
            }
            for u in window
        ],
        "has_more": has_more,
        "next_offset": (offset + limit) if has_more else None,
    })
