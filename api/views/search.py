

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import (
    Case, Count, IntegerField, Q, Value, When,
)
from django.utils.dateparse import parse_datetime

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response



from ..models import (
    BlockedUser, Follow, MutedUser, Page, PageFollow, Post,
    PostLike, SavedPost, SearchHistory,
)
from ..serializers import (
    BasicPageSerializer, BasicUserSerializer,
)
from ..utils import (
    decode_cursor,
    encode_cursor,
    log_activity,
)
from ..post_media import ordered_media
from ..services.feed_helpers import (
    get_muted_page_ids, post_visibility_q,
    likes_count_subquery, comments_count_subquery, saves_count_subquery,
)


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


@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAuthenticated])
def search_history(request):
    user = request.user

    # ── GET: return last 20 history entries ──────────────────────────────
    if request.method == "GET":
        entries = SearchHistory.objects.filter(user=user).select_related(
            "searched_user__userprofile", "searched_page"
        )[:20]

        results = []
        for e in entries:
            if e.query:
                results.append({"id": e.id, "kind": "query", "query": e.query})

            elif e.searched_user:
                u = e.searched_user
                avatar = None
                if hasattr(u, "userprofile") and u.userprofile.avatar:
                    avatar = request.build_absolute_uri(u.userprofile.avatar.url)
                results.append({
                    "id": e.id,
                    "kind": "user",
                    "user": {
                        "id": u.id,
                        "username": u.username,
                        "avatar": avatar,
                    },
                })

            elif e.searched_page:
                p = e.searched_page
                avatar = None
                if p.avatar:
                    avatar = request.build_absolute_uri(p.avatar.url)
                results.append({
                    "id": e.id,
                    "kind": "page",
                    "page": {
                        "id": p.id,
                        "name": p.name,
                        "is_private": p.is_private,
                        "avatar": avatar,
                    },
                })
            else:
                # Entry has no query, user, or page (e.g. the referenced record
                # was deleted and the FK was set to NULL). Skip silently.
                pass

        return Response(results)

    # ── POST: save a new history entry ───────────────────────────────────
    if request.method == "POST":
        kind = request.data.get("kind")           # "query" | "user" | "page"
        query = request.data.get("query", "").strip()
        target_id = request.data.get("id")        # user_id or page_id

        # Canonical, case-folded form for the analytics / ranking signal so
        # "Vintage Cars" and "vintage cars" collapse to ONE search term
        # (ACTIVITY_AND_FEED_AUDIT.md item A15). The original `query` is kept
        # verbatim on the SearchHistory rows below so the recent-searches UI
        # shows the user exactly what they typed.
        query_norm = query.lower()

        with transaction.atomic():
            if kind == "query":
                if not query:
                    return Response({"error": "query required"}, status=400)

                # Upsert: if the same query already exists, move it to the top
                # by deleting the old one and re-creating it. Dedup is
                # case-INsensitive (iexact) so the recent-searches list doesn't
                # carry "Vintage Cars" and "vintage cars" as two entries; the
                # freshly-created row keeps the latest original casing.
                SearchHistory.objects.filter(user=user, query__iexact=query).delete()
                entry = SearchHistory.objects.create(user=user, query=query)
                log_activity(user, "search_query", query=query_norm)

            elif kind == "user":
                # Coerce + existence-check before INSERT so bad input returns
                # 400/404 instead of crashing the FK constraint with a 500.
                try:
                    target_id = int(target_id)
                except (TypeError, ValueError):
                    return Response({"error": "id required"}, status=400)
                if not User.objects.filter(id=target_id).exists():
                    return Response({"error": "user not found"}, status=404)
                SearchHistory.objects.filter(
                    user=user, searched_user_id=target_id
                ).delete()
                entry = SearchHistory.objects.create(
                    user=user, searched_user_id=target_id
                )
                # Clicking a user from search = search_click
                log_activity(
                    user, "search_click",
                    target_user_id=target_id,
                    query=query_norm,
                    surface="search_results",
                    channel="search",
                )

            elif kind == "page":
                try:
                    target_id = int(target_id)
                except (TypeError, ValueError):
                    return Response({"error": "id required"}, status=400)
                if not Page.objects.filter(id=target_id).exists():
                    return Response({"error": "page not found"}, status=404)
                SearchHistory.objects.filter(
                    user=user, searched_page_id=target_id
                ).delete()
                entry = SearchHistory.objects.create(
                    user=user, searched_page_id=target_id
                )
                # Clicking a page from search = search_click
                log_activity(
                    user, "search_click",
                    page_id=target_id,
                    query=query_norm,
                    surface="search_results",
                    channel="search",
                )

            else:
                return Response({"error": "invalid kind"}, status=400)

            # Keep only the 50 most recent entries per user
            old_ids = list(
                SearchHistory.objects.filter(user=user)
                .values_list("id", flat=True)[50:]
            )
            if old_ids:
                SearchHistory.objects.filter(id__in=old_ids).delete()

        return Response({"id": entry.id}, status=201)

    # ── DELETE: remove a single entry by id ──────────────────────────────
    if request.method == "DELETE":
        try:
            entry_id = int(request.data.get("id"))
        except (TypeError, ValueError):
            return Response({"error": "id required"}, status=400)
        SearchHistory.objects.filter(user=user, id=entry_id).delete()
        return Response(status=204)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def search_posts(request):
    user = request.user
    q = request.query_params.get("q", "").strip()

    if not q:
        return Response({"results": [], "has_more": False, "next_cursor": None})

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    # --- blocked / muted guards (same as explore) ---
    blocked_pairs = BlockedUser.objects.involving(user).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_ids = set()
    for u, b in blocked_pairs:
        blocked_ids.add(u)
        blocked_ids.add(b)
    blocked_ids.discard(user.id)

    muted_user_ids = MutedUser.objects.filter(
        user=user
    ).values_list("muted_user_id", flat=True)

    muted_page_ids = get_muted_page_ids(user)

    # Followed users / pages — feed into the canonical visibility filter.
    # Pages the viewer owns are treated as followed so owners always see
    # their own page's posts in search results.
    followed_user_ids = set(
        Follow.objects.filter(follower=user).values_list("following_id", flat=True)
    )
    followed_page_ids = set(
        PageFollow.objects.filter(user=user).values_list("page_id", flat=True)
    ) | set(
        Page.objects.filter(owner=user).values_list("id", flat=True)
    )

    liked_post_ids = set(
        PostLike.objects.filter(user=user).values_list("post_id", flat=True)
    )
    saved_post_ids = set(
        SavedPost.objects.filter(user=user).values_list("post_id", flat=True)
    )

    # Match on: post description · author username · page name
    posts = (
        Post.objects
        .filter(
            Q(description__icontains=q)
            | Q(user__username__icontains=q)
            | Q(page__name__icontains=q)
        )
        # Canonical visibility filter — replaces an earlier rule that only
        # excluded `is_private` pages, leaving super-private pages and
        # private-account personal posts visible in search results.
        .filter(post_visibility_q(user, followed_user_ids, followed_page_ids))
        .exclude(user_id__in=blocked_ids)
        .exclude(user_id__in=muted_user_ids)
        .exclude(page_id__in=muted_page_ids)
        .annotate(
            media_count=Count("media", distinct=True),
            likes_count=likes_count_subquery(),
            comments_count=comments_count_subquery(),
            saves_count=saves_count_subquery(),
        )
        .filter(media_count__gt=0)
        .distinct()
        .select_related("user", "user__userprofile", "page")
        .prefetch_related("media")
        .order_by("-created_at", "-id")
    )

    # Keyset: posts strictly older than the cursor. The compound (created_at,
    # id) comparison keeps ordering total/stable when posts share a timestamp,
    # so no post is skipped or repeated across pages.
    cursor = decode_cursor(request.query_params.get("cursor"))
    last_created = parse_datetime(cursor["created_at"]) if cursor.get("created_at") else None
    last_id = cursor.get("id")
    if last_created is not None and last_id is not None:
        posts = posts.filter(
            Q(created_at__lt=last_created)
            | Q(created_at=last_created, id__lt=last_id)
        )

    # Fetch one extra row to detect `has_more` without a second COUNT query.
    posts = list(posts[: limit + 1])
    has_more = len(posts) > limit
    posts = posts[:limit]

    result = []
    for post in posts:
        media_qs = ordered_media(post)
        first = media_qs[0]

        is_single_video = (
            len(media_qs) == 1
            and first.file.name.lower().endswith((".mp4", ".mov", ".webm"))
        )

        up = getattr(post.user, "userprofile", None)

        result.append({
            "id": post.id,
            "description": post.description,
            "created_at": post.created_at,
            "is_single_video": is_single_video,
            "media": [
                request.build_absolute_uri(
                    m.thumbnail.url if m.thumbnail else m.file.url
                )
                for m in media_qs
            ],
            "video": request.build_absolute_uri(first.file.url),
            "user": {
                "id": post.user.id,
                "username": post.user.username,
                "avatar": (
                    request.build_absolute_uri(up.avatar.url)
                    if up and up.avatar
                    else None
                ),
            },
            "page": (
                {
                    "id": post.page.id,
                    "name": post.page.name,
                    "avatar": (
                        request.build_absolute_uri(post.page.avatar.url)
                        if post.page.avatar
                        else None
                    ),
                }
                if post.page
                else None
            ),
            "likes_count": post.likes_count,
            "comments_count": post.comments_count,
            "saves_count": post.saves_count,
            "is_liked": post.id in liked_post_ids,
            "is_saved": post.id in saved_post_ids,
        })

    next_cursor = None
    if has_more and posts:
        last = posts[-1]
        next_cursor = encode_cursor({
            "created_at": last.created_at.isoformat(),
            "id": last.id,
        })

    return Response({
        "results": result,
        "has_more": has_more,
        "next_cursor": next_cursor,
    })


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
