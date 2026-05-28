"""User directory listing (`list_users`)."""


from django.contrib.auth.models import User
from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...services.pagination import decode_cursor, encode_cursor


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_users(request):
    """
    Paginated directory of users (everyone except the caller).

    Keyset/cursor pagination ordered by (username, id). The endpoint used to
    return EVERY user in the system in a single response; on a real user base
    that response grows without bound. Username is unique (and indexed), so the
    keyset stays cheap at any depth and is stable as accounts are created or
    deleted between pages.

    GET params:
      limit  — page size (default 30, capped at 60)
      cursor — opaque token from the previous page's `next_cursor`

    Response: { "results": [...], "has_more": bool, "next_cursor": str|null }
    """
    try:
        limit = int(request.query_params.get("limit", 30))
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(limit, 60))

    qs = (
        User.objects
        .exclude(id=request.user.id)
        .order_by("username", "id")
    )

    # Keyset: only rows strictly after the cursor's (username, id). Compound
    # comparison so usernames that somehow collide can't drop or repeat a row.
    cursor = decode_cursor(request.query_params.get("cursor"))
    last_username = cursor.get("username")
    last_id = cursor.get("id")
    if last_username is not None and last_id is not None:
        qs = qs.filter(
            Q(username__gt=last_username)
            | Q(username=last_username, id__gt=last_id)
        )

    # Fetch one extra row to detect `has_more` without a second COUNT query.
    rows = list(qs[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]

    results = [{"id": u.id, "username": u.username} for u in rows]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor({"username": last.username, "id": last.id})

    return Response({
        "results": results,
        "has_more": has_more,
        "next_cursor": next_cursor,
    })


