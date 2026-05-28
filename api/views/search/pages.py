"""Page search (`search_pages`)."""


from django.db.models import Case, IntegerField, Q, Value, When
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Page, PageFollow


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def search_pages(request):
    user = request.user
    query = request.query_params.get('q', '')

    # Offset pagination: results are ranked by a computed `priority` (owner /
    # follower / other) then name, so a keyset cursor doesn't apply cleanly —
    # offset is the right fit and the result set is bounded by the query.
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

    # 1. Get IDs of pages the user follows
    followed_page_ids = PageFollow.objects.filter(
        user=user
    ).values_list('page_id', flat=True)

    # 2. Base Queryset
    #
    # Visibility rules for the upload-target picker:
    #   - Public pages                              → always shown
    #   - Private OR super-private pages            → shown ONLY if the
    #     viewer owns the page, or already follows the page
    #
    # Implemented as an exclude(): drop any page that is private/super-private
    # AND the viewer is neither owner nor follower.
    if query:
        base_qs = Page.objects.filter(name__icontains=query)
    else:
        base_qs = Page.objects.all()

    base_qs = base_qs.exclude(
        (Q(is_private=True) | Q(is_super_private=True))
        & ~Q(owner=user)
        & ~Q(id__in=followed_page_ids)
    )

    # 3. Apply Priority Logic using Annotation
    # Priority 1: User is Owner
    # Priority 2: User Follows
    # Priority 3: Everything else
    #
    # select_related("owner") joins the owner User row into the same SELECT,
    # so the response loop's `page.owner.username` read is free. Without it,
    # the loop fires +1 query per page (up to 30 wasted round trips per
    # search keystroke once debouncing is in play).
    pages = (
        base_qs.select_related("owner")
        .annotate(
            priority=Case(
                When(owner=user, then=Value(1)),
                When(id__in=followed_page_ids, then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            )
        )
        .order_by('priority', 'name', 'id')  # priority, then alphabetical, id tiebreak
    )

    # Offset window: fetch one extra row to detect `has_more` without a COUNT.
    page_window = list(pages[offset : offset + limit + 1])
    has_more = len(page_window) > limit
    page_window = page_window[:limit]

    # 4. Format Response
    data = [
        {
            "id": page.id,
            "name": page.name,
            "avatar": request.build_absolute_uri(page.avatar.url) if page.avatar else None,
            "owner": page.owner.username,
            "relationship": "owner" if page.owner == user else ("following" if page.id in followed_page_ids else "none")
        }
        for page in page_window
    ]

    return Response({
        "results": data,
        "has_more": has_more,
        "next_offset": (offset + limit) if has_more else None,
    })
