
from collections import defaultdict

from django.core.cache import cache
from django.db.models import (
    Count, Exists, OuterRef, Prefetch,
)

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response



from ..models import (
    Follow, Post, PostLike, PostMedia, PostMediaTag, SavedPost,
)
from ..post_media import ordered_media
from ..services.feed_helpers import (
    build_feed_context, post_visibility_q, serialize_post,
    likes_count_subquery, comments_count_subquery, saves_count_subquery,
)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def home_feed(request):
    """
    Home feed -- four-rail composition.

    The actual ranking pipeline lives in `feed_ranking.compose_home_feed_page`
    (see docs/FEED_RANKING_SPEC.md). This view is a thin wrapper that supplies
    `serialize_post` and `build_feed_context` -- both of which still live here
    -- to the pipeline, then wraps the dict result in a DRF Response.

    Cursor: the response shape is `{"results": [...], "next": URL,
    "following_count": N}`. The cursor format is a single base64-encoded
    JSON token in the `cursor` query param. Old `cursor` / `cursor_id` /
    `s_offset` params from clients on the previous version are ignored
    cleanly (decode_cursor falls through to {} on malformed input, which
    is the same as a fresh load).
    """
    from ..feed import compose_home_feed_page

    # compose_home_feed_page already builds the feed context internally and
    # now folds `following_count` into the payload directly, so this view
    # no longer needs a second build_feed_context lookup. Used to be two
    # cache hits per /feed/ request — now one.
    payload = compose_home_feed_page(
        request=request,
        user=request.user,
        serialize_post_fn=serialize_post,
        build_feed_context_fn=build_feed_context,
    )

    return Response(payload)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def explore_feed(request):
    """
    Explore fallback feed.

    Two-phase fetch (matches the pattern in get_suggested_feed):
      1. CANDIDATES: fetch a small pool of recent post IDs + user_ids for
         scoring. Score by mutual-overlap with the viewer's social graph.
         Cache the ranked (id, score) list per user for 5 minutes.
      2. HYDRATE: fetch only the page slice with the full annotations +
         prefetches the response actually needs.

    Previously this view materialized 400 full Post objects, serialized all
    of them, then sliced to 120. That paid the prefetch + serialization
    cost for 280 posts that were thrown away on every hit.
    """
    user = request.user

    try:
        limit = int(request.query_params.get("limit", 30))
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(limit, 60))

    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    # ---------------- SHARED PER-USER SETS ----------------
    # build_feed_context is cached per user for 90 s and is also the source
    # of truth used by /feed/. Reusing it here lets explore piggyback on the
    # same cache and removes ~6 redundant DB reads on every hit.
    context = build_feed_context(user)

    blocked_ids        = context["blocked_user_ids"]
    muted_user_ids     = context["muted_user_ids"]
    muted_page_ids     = context["muted_page_ids"]
    my_followers       = context["viewer_followers"]
    my_following       = context["viewer_following"]
    my_following_pages = context["followed_pages"]

    # Tuning knobs.
    EXPLORE_TTL_S      = 300   # match get_suggested_feed
    CANDIDATE_POOL_CAP = 150   # was 400 — the client only ever displays a
                               # handful on the empty-feed fallback before
                               # the user follows someone or navigates away.
    PAGE_SIZE          = 120

    cache_key = f"explore_scores:{user.id}"
    scored_ids = cache.get(cache_key)

    if scored_ids is None:
        # ---------------- COMPUTE: cheap candidate fetch ----------------
        # Only the columns needed to score. No prefetches yet — full post
        # objects are hydrated below for the top-N only.
        candidate_pool = list(
            Post.objects
            .filter(post_visibility_q(user, my_following, my_following_pages))
            .exclude(user_id__in=blocked_ids)
            .exclude(user_id__in=muted_user_ids)
            .exclude(page_id__in=muted_page_ids)
            .annotate(media_count=Count("media", distinct=True))
            .filter(media_count__gt=0)
            .distinct()
            .order_by("-created_at")
            .values("id", "user_id")[:CANDIDATE_POOL_CAP]
        )

        owner_ids = {c["user_id"] for c in candidate_pool}

        mutual_followers_by_owner = defaultdict(int)
        if owner_ids and my_followers:
            for following_id in (
                Follow.objects
                .filter(following_id__in=owner_ids, follower_id__in=my_followers)
                .values_list("following_id", flat=True)
            ):
                mutual_followers_by_owner[following_id] += 1

        mutual_following_by_owner = defaultdict(int)
        if owner_ids and my_following:
            for follower_id in (
                Follow.objects
                .filter(follower_id__in=owner_ids, following_id__in=my_following)
                .values_list("follower_id", flat=True)
            ):
                mutual_following_by_owner[follower_id] += 1

        scored = []
        for c in candidate_pool:
            mutual_followers = mutual_followers_by_owner.get(c["user_id"], 0)
            mutual_following = mutual_following_by_owner.get(c["user_id"], 0)
            score = mutual_followers * 1.3 + mutual_following * 1.0
            scored.append((c["id"], score))

        scored.sort(key=lambda x: x[1], reverse=True)
        scored_ids = scored[:PAGE_SIZE]
        cache.set(cache_key, scored_ids, timeout=EXPLORE_TTL_S)

    if not scored_ids:
        return Response({"results": [], "has_more": False, "next_offset": None})

    # Page window over the cached ranked list — hydrate only this slice instead
    # of all PAGE_SIZE rows. `has_more` is known from the cached list length,
    # so paging never recomputes or re-scores.
    page_window = scored_ids[offset : offset + limit]
    has_more = (offset + limit) < len(scored_ids)

    if not page_window:
        return Response({"results": [], "has_more": has_more, "next_offset": None})

    # ---------------- HYDRATE: full fetch for the page slice ----------------
    page_ids = [pid for pid, _ in page_window]

    posts_qs = (
        Post.objects
        .filter(id__in=page_ids)
        .annotate(
            likes_count=likes_count_subquery(),
            comments_count=comments_count_subquery(),
            saves_count=saves_count_subquery(),
            viewer_liked=Exists(
                PostLike.objects.filter(post=OuterRef("pk"), user=user)
            ),
            viewer_saved=Exists(
                SavedPost.objects.filter(post=OuterRef("pk"), user=user)
            ),
        )
        .select_related("user", "user__userprofile", "page")
        .prefetch_related(
            Prefetch("media", queryset=PostMedia.objects.order_by("order")),
            Prefetch("media__tags", queryset=PostMediaTag.objects.select_related("user")),
        )
    )
    post_map = {p.id: p for p in posts_qs}

    ranked = []
    for pid, score in page_window:
        post = post_map.get(pid)
        # Posts can disappear between the cache write and the read
        # (deletion, becoming private). Skip silently — the client just
        # sees a slightly shorter page until the cache turns over.
        if post is None:
            continue

        media_qs = ordered_media(post)
        if not media_qs:
            continue
        first = media_qs[0]

        is_single_video = (
            len(media_qs) == 1
            and first.file.name.lower().endswith((".mp4", ".mov", ".webm"))
        )

        up = getattr(post.user, "userprofile", None)

        ranked.append({
            "id": post.id,
            "score": score,
            "description": post.description,
            "created_at": post.created_at,
            "is_single_video": is_single_video,

            "media": [
                {
                    "id": m.id,
                    "file": request.build_absolute_uri(m.file.url),
                    "thumbnail": (
                        request.build_absolute_uri(m.thumbnail.url)
                        if m.thumbnail else None
                    ),
                    "order": m.order,
                    "tags": [
                        {"id": t.user.id, "username": t.user.username}
                        for t in m.tags.all()
                    ],
                }
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
                    "is_private": post.page.is_private,
                }
                if post.page
                else None
            ),

            "likes_count": post.likes_count,
            "comments_count": post.comments_count,
            "saves_count": post.saves_count,
            "is_liked": bool(post.viewer_liked),
            "is_saved": bool(post.viewer_saved),
            "is_owner": post.user_id == user.id,
            "is_followed": post.user_id in my_following,
            "is_page_followed": (
                post.page_id in my_following_pages
                if post.page_id is not None
                else False
            ),
            "suggested": True,
            "is_public_override": post.is_public_override,
            "top_comments": [],
        })

    return Response({
        "results": ranked,
        "has_more": has_more,
        "next_offset": (offset + limit) if has_more else None,
    })
