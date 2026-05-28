"""Combined search-bar endpoint (`search`): users + pages typeahead."""


from django.contrib.auth.models import User
from django.db.models import Case, IntegerField, Q, Value, When
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Page, PageFollow
from ...serializers import BasicPageSerializer, BasicUserSerializer
from ...services.feed_helpers import get_muted_page_ids


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def search(request):
    user = request.user
    q = request.query_params.get("q", "").strip()

    if not q:
        return Response({
            "users": [],
            "pages": []
        })

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
    # 🔕 MUTED PAGES (one-directional)
    # --------------------------------------------------
    muted_page_ids = get_muted_page_ids(user)

    # --------------------------------------------------
    # 🔍 SEARCH USERS (excluding blocked)
    # --------------------------------------------------
    # Match on username OR display name (first/last). Username matches
    # are ranked above display-name matches via `match_priority` so a
    # user typing someone's handle gets the exact handle at the top
    # instead of being out-ranked by a coincidental name match.
    #
    # Performance: this ILIKE substring match across username + first/last name
    # is index-accelerated on PostgreSQL by the pg_trgm GIN indexes added in
    # migration 0092 (UB-2) -- gin_trgm_ops serves ILIKE '%q%' for queries of
    # >= 3 chars, so this no longer table-scans as the user table grows. (On the
    # SQLite dev fallback there's no trigram index and it scans -- fine for dev.)
    users = (
        User.objects
        .filter(
            Q(username__icontains=q)
            | Q(userprofile__first_name__icontains=q)
            | Q(userprofile__last_name__icontains=q)
        )
        .exclude(id__in=blocked_user_ids)
        .annotate(
            match_priority=Case(
                When(username__icontains=q, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .select_related("userprofile")
        .order_by("match_priority", "username")
        .distinct()[:10]
    )

    # --------------------------------------------------
    # 🔍 SEARCH PAGES (excluding muted and super private)
    # --------------------------------------------------
    followed_page_ids_set = set(
        PageFollow.objects.filter(user=user).values_list("page_id", flat=True)
    )
    owned_page_ids_set = set(
        Page.objects.filter(owner=user).values_list("id", flat=True)
    )

    pages = (
        Page.objects
        .filter(name__icontains=q)
        .exclude(id__in=muted_page_ids)
        .exclude(
            # Exclude private / super-private pages the viewer can't access.
            # Visibility parity with search_pages: a page marked private OR
            # super-private is hidden from anyone who isn't the owner and
            # isn't already a follower. Previously only is_super_private was
            # checked here, which meant is_private pages leaked into the
            # global search results to non-followers.
            (Q(is_private=True) | Q(is_super_private=True))
            & ~Q(owner=user)
            & ~Q(id__in=followed_page_ids_set)
        )
        .order_by("name")[:10]
    )

    # --------------------------------------------------
    # 🧾 SERIALIZE
    # --------------------------------------------------
    ctx = {'request': request}
    return Response({
        "users": BasicUserSerializer(users, many=True, context=ctx).data,
        "pages": BasicPageSerializer(pages, many=True, context=ctx).data,
    })


