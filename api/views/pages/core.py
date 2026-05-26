"""Page core: create, detail, follow toggle, listing, avatar, mute, and settings."""


from django.core.cache import cache
from django.db.models import Exists, F, OuterRef, Prefetch, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Memory, MutedPage, Notification, Page, PageFollow, PageFollowRequest, PinnedPage, Post, PostLike, PostMedia, SavedPost
from ...serializers import PageDetailSerializer
from ...services.feed_helpers import (
    get_muted_page_ids,
    likes_count_subquery, comments_count_subquery, saves_count_subquery,
)
from ...services.media_processing import validate_image_upload
from ...utils import push_to_user

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_page(request):
    name = request.data.get('name')
    description = request.data.get('description', '')
    is_private = request.data.get('is_private', False)

    if not name:
        return Response(
            {"error": "Page name is required"},
            status=status.HTTP_400_BAD_REQUEST
        )

    page = Page.objects.create(
        owner=request.user,
        name=name,
        description=description,
        is_private=is_private
    )

    return Response(
        {
            "id": page.id,
            "name": page.name,
            "is_private": page.is_private,
        },
        status=status.HTTP_201_CREATED
    )



@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_page_detail(request):
    page_id = request.query_params.get('page_id')

    # Sort order for the grid. Anything other than the three known values
    # falls through to "latest" so the API stays well-defined for old clients.
    sort = request.query_params.get('sort', 'latest')
    if sort not in {"latest", "popular", "oldest"}:
        sort = "latest"

    # Pagination — default page size matches the 3x3 grid on the client.
    # Clamp both values to keep clients from asking for unbounded slices.
    try:
        limit = int(request.query_params.get('limit', 9))
    except (TypeError, ValueError):
        limit = 9
    try:
        offset = int(request.query_params.get('offset', 0))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 60))
    offset = max(0, offset)

    if not page_id:
        return Response(
            {"error": "page_id is required"},
            status=400
        )

    page = get_object_or_404(Page, id=page_id)
    viewer = request.user

    # --------------------------------------------------
    # 👑 OWNERSHIP / PERMISSIONS
    # --------------------------------------------------
    is_owner = page.owner == viewer

    # --------------------------------------------------
    # 🔒 SUPER PRIVATE — block access for non-owners/non-followers
    # --------------------------------------------------
    if page.is_super_private and not is_owner:
        is_following_early = PageFollow.objects.filter(
            user=viewer, page=page
        ).exists()
        if not is_following_early:
            return Response(
                {"error": "unavailable", "super_private": True},
                status=403,
            )

    # --------------------------------------------------
    # 🔕 MUTED PAGE STATUS
    # --------------------------------------------------
    is_muted = page.id in get_muted_page_ids(viewer)

    # --------------------------------------------------
    # 👥 FOLLOW STATUS
    # --------------------------------------------------
    is_following = PageFollow.objects.filter(
        user=viewer,
        page=page
    ).exists()

    # Pending follow request (private pages only — admins haven't acted yet)
    has_requested_follow = (
        page.is_private
        and not is_following
        and PageFollowRequest.objects.filter(
            requester=viewer,
            page=page,
        ).exists()
    )

    followers_count = PageFollow.objects.filter(
        page=page
    ).count()

    # --------------------------------------------------
    # 🧠 MEMORY STATUS
    # --------------------------------------------------
    is_in_memories = Memory.objects.filter(
        user=viewer,
        page=page
    ).exists()

    # --------------------------------------------------
    # 📌 PINNED STATUS
    # Whether the viewer has pinned this page to their own profile. Drives
    # the filled/active state of the Pin button in the Page action menu.
    # --------------------------------------------------
    is_pinned = PinnedPage.objects.filter(
        user=viewer,
        page=page
    ).exists()

    # --------------------------------------------------
    # 🔐 PAGE VISIBILITY
    # --------------------------------------------------
    can_view_posts = (
        not page.is_private
        or is_owner
        or is_following
    )

    # --------------------------------------------------
    # ✍️ POSTING PERMISSION
    # --------------------------------------------------
    can_post = (
        is_owner
        or page.anyone_can_post
    )

    # --------------------------------------------------
    # 📰 POSTS
    # --------------------------------------------------
    posts_data = []
    has_more = False

    if can_view_posts:
        # Compute counts and per-viewer flags in SQL so we don't issue
        # ~5 extra queries per post when serializing. Media is prefetched
        # in `order` order so the list comprehension below doesn't need
        # to re-sort (and doesn't trigger an extra query per post).
        viewer_liked = PostLike.objects.filter(
            post=OuterRef("pk"), user=viewer,
        )
        viewer_saved = SavedPost.objects.filter(
            post=OuterRef("pk"), user=viewer,
        )

        posts_qs = (
            Post.objects
            .filter(page=page)
            .select_related("user", "user__userprofile")
            .prefetch_related(
                Prefetch(
                    "media",
                    queryset=PostMedia.objects.order_by("order"),
                    to_attr="ordered_media",
                )
            )
            .annotate(
                _likes=likes_count_subquery(),
                _comments=comments_count_subquery(),
                _saves=saves_count_subquery(),
                _is_liked=Exists(viewer_liked),
                _is_saved=Exists(viewer_saved),
            )
        )

        # Order the *full* queryset in SQL so the chosen sort holds across
        # the whole post set, not just the slice we're about to serialize.
        if sort == "oldest":
            ordered = posts_qs.order_by("created_at", "id")
        elif sort == "popular":
            ordered = posts_qs.annotate(
                popularity=F("_likes") + F("_comments") + F("_saves")
            ).order_by("-popularity", "-created_at", "-id")
        else:  # "latest"
            ordered = posts_qs.order_by("-created_at", "-id")

        # Pagination — fetch limit+1 so we can detect `has_more` without a
        # separate COUNT(*). Drop the extra row before serializing.
        fetched = list(ordered[offset:offset + limit + 1])
        has_more = len(fetched) > limit
        posts = fetched[:limit]

        posts_data = [
            {
                "id": p.id,
                "description": p.description,
                "created_at": p.created_at,
                "media": [
                    {
                        "id": m.id,
                        "file": request.build_absolute_uri(m.file.url),
                        "thumbnail": (
                            request.build_absolute_uri(m.thumbnail.url)
                            if m.thumbnail else None
                        ),
                        "order": m.order,
                    }
                    for m in p.ordered_media
                ],
                "user": {
                    "id": p.user.id,
                    "username": p.user.username,
                    "avatar": (
                        request.build_absolute_uri(p.user.userprofile.avatar.url)
                        if hasattr(p.user, "userprofile") and p.user.userprofile.avatar
                        else None
                    ),
                },
                "likes_count": p._likes,
                "comments_count": p._comments,
                "saves_count": p._saves,
                "is_liked": p._is_liked,
                "is_saved": p._is_saved,
                "is_owner": p.user_id == viewer.id,
                "is_public_override": p.is_public_override,
            }
            for p in posts
        ]

    # --------------------------------------------------
    # 📦 RESPONSE
    # --------------------------------------------------
    return Response({
        **PageDetailSerializer(
            page,
            context={
                'request': request,
                'is_owner': is_owner,
                'can_post': can_post,
                'is_following': is_following,
                'has_requested_follow': has_requested_follow,
                'followers_count': followers_count,
                'is_muted': is_muted,
                'is_in_memories': is_in_memories,
                'is_pinned': is_pinned,
            },
        ).data,
        "posts": posts_data,
        "has_more": has_more,
    })



@api_view(['POST'])
@permission_classes([IsAuthenticated])
def toggle_page_follow(request):
    page_id = request.data.get('page_id')

    if not page_id:
        return Response({"error": "page_id is required"}, status=400)

    page = get_object_or_404(Page, id=page_id)
    user = request.user

    # Already following → unfollow
    existing = PageFollow.objects.filter(user=user, page=page)
    if existing.exists():
        existing.delete()
        # build_feed_context bakes followed_pages into feed_ctx, and the
        # suggested feed key derives from it indirectly. Without these the
        # next feed load can keep showing the unfollowed page's posts as
        # follow-feed material (up to 90 s) and the suggested feed for up
        # to 5 min. Mirrors toggle_follow / toggle_page_mute.
        cache.delete(f"feed_ctx:{user.id}")
        cache.delete(f"suggested_feed_scores:{user.id}")
        return Response({"status": "unfollowed"})

    # 🚫 BLOCK CHECK — mirrors toggle_follow (views/follow.py): if the page
    # owner has blocked you, or you have blocked the page owner, you cannot
    # start (or request) following. The unfollow path above is allowed
    # regardless, since unfollowing only cleans up an existing relationship.
    # Skip when user == owner (owners always "follow" their own page from
    # the UI's perspective — there's no block with yourself).
    if page.owner_id != user.id and BlockedUser.objects.between(
        page.owner, user
    ).exists():
        return Response({"error": "Not allowed"}, status=403)

    # Private page → request (or cancel pending request)
    if page.is_private:
        existing_request = PageFollowRequest.objects.filter(
            requester=user,
            page=page,
        )
        if existing_request.exists():
            # Second tap while a request is pending → cancel the request.
            existing_request.delete()
            # Clean up the matching pending notification so the admin's
            # inbox doesn't keep a stale request item.
            Notification.objects.filter(
                recipient=page.owner,
                actor=user,
                notification_type="page_follow_request",
                page=page,
            ).delete()
            return Response({"status": "request_cancelled"})

        PageFollowRequest.objects.create(
            requester=user,
            page=page,
        )
        Notification.objects.create(
            recipient=page.owner,
            actor=user,
            notification_type="page_follow_request",
            page=page,
        )
        push_to_user(
            page.owner,
            title="New follow request",
            body=f"{user.username} wants to follow {page.name}",
            extra_data={
                "type": "page_follow_request",
                "page_id": page.id,
                "actor_id": user.id,
            },
        )
        return Response({"status": "requested"})

    # Public page → follow immediately
    PageFollow.objects.create(user=user, page=page)
    # Invalidate feed caches that bake in followed_pages — without this the
    # newly followed page's posts won't show in the home feed for up to 90s
    # and the suggested feed continues to suggest the page for up to 5 min.
    cache.delete(f"feed_ctx:{user.id}")
    cache.delete(f"suggested_feed_scores:{user.id}")
    # Notify page owner
    if page.owner != user:
        Notification.objects.create(
            recipient=page.owner,
            actor=user,
            notification_type="page_follow",
            page=page,
        )
        push_to_user(
            page.owner,
            title="New follower",
            body=f"{user.username} started following {page.name}",
            extra_data={
                "type": "page_follow",
                "page_id": page.id,
                "actor_id": user.id,
            },
        )
    return Response({"status": "following"})



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_pages(request):
    # Paginate: the original endpoint returned every Page row in a single
    # response, which would scale to a full-table dump as the platform grows.
    # Offset/limit matches the rest of the codebase (e.g. views/profile.py:108).
    try:
        limit = int(request.query_params.get('limit', 30))
    except (TypeError, ValueError):
        limit = 30
    try:
        offset = int(request.query_params.get('offset', 0))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 60))
    offset = max(0, offset)

    # 🔒 SUPER PRIVATE — never include super-private pages in this global
    # enumeration unless the viewer owns the page or already follows it.
    # `get_page_detail` already 403s a non-owner/non-follower viewer on
    # super-private pages (see line 238); leaving them in the global list
    # would let a viewer discover that the page exists at all, which is
    # exactly what `is_super_private` is supposed to prevent.
    viewer = request.user
    visible_super_private_ids = set(
        PageFollow.objects
        .filter(user=viewer)
        .values_list("page_id", flat=True)
    ) | set(
        Page.objects
        .filter(owner=viewer)
        .values_list("id", flat=True)
    )

    pages = (
        Page.objects
        .filter(
            Q(is_super_private=False)
            | Q(id__in=visible_super_private_ids)
        )
        .order_by('-created_at')[offset:offset + limit]
    )

    data = [
        {
            "id": page.id,
            "name": page.name,
            "is_private": page.is_private,
        }
        for page in pages
    ]

    return Response(data)



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_page_avatar(request):
    page_id = request.data.get("page_id")
    avatar   = request.FILES.get("avatar")

    if not page_id or not avatar:
        return Response({"error": "page_id and avatar required"}, status=400)

    page = get_object_or_404(Page, id=page_id)

    if page.owner != request.user:
        return Response({"error": "Not allowed"}, status=403)

    # Validate the upload (content-type, size cap, magic-byte sniff) the same
    # way post/comment media is validated, so an arbitrary client file can't be
    # written straight to media/page_avatars/. ImageField.save() alone does not
    # run this check (M2).
    try:
        validate_image_upload(avatar)
    except ValueError as e:
        return Response({"error": str(e)}, status=400)

    page.avatar = avatar
    page.save(update_fields=["avatar"])

    return Response({
        "status": "ok",
        "avatar": request.build_absolute_uri(page.avatar.url),
    })



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_page_mute(request):
    page_id = request.data.get("page_id")

    if not page_id:
        return Response(
            {"error": "page_id required"},
            status=400
        )

    page = get_object_or_404(Page, id=page_id)
    user = request.user

    mute = MutedPage.objects.filter(
        user=user,
        page=page
    ).first()

    if mute:
        mute.delete()
        return Response({"status": "unmuted"})
    else:
        MutedPage.objects.create(
            user=user,
            page=page
        )
        # Invalidate so the muted page disappears from the next feed load
        cache.delete(f"feed_ctx:{user.id}")
        cache.delete(f"suggested_feed_scores:{user.id}")
        return Response({"status": "muted"})



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_page_pin(request):
    """Pin / unpin a page to the caller's own profile.

    A pin is a (user, page) row; the caller's profile shows their pinned
    pages as a row of circular avatars under the Follow / Message buttons
    (Instagram-highlights style). Tapping the button again removes the pin.
    There is no cap on the number of pins — the row scrolls horizontally.

    Mirrors toggle_page_mute's shape: idempotent toggle keyed on the
    unique (user, page) constraint, returning the resulting state.
    """
    page_id = request.data.get("page_id")

    if not page_id:
        return Response(
            {"error": "page_id required"},
            status=400
        )

    page = get_object_or_404(Page, id=page_id)
    user = request.user

    existing = PinnedPage.objects.filter(
        user=user,
        page=page
    ).first()

    if existing:
        existing.delete()
        return Response({"status": "unpinned"})

    # get_or_create guards against a double-tap racing two inserts past the
    # unique_together constraint (which would otherwise 500 on IntegrityError).
    PinnedPage.objects.get_or_create(user=user, page=page)
    return Response({"status": "pinned"})



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_page_settings(request):
    page_id = request.data.get("page_id")
    key = request.data.get("key")
    value = request.data.get("value")

    page = get_object_or_404(Page, id=page_id)

    if page.owner != request.user:
        return Response({"error": "Not allowed"}, status=403)

    # --------------------------------------------------
    # ✅ PER-KEY VALIDATION
    # --------------------------------------------------
    # Each allowed key has its own coercion + bounds check. We refuse
    # unknown keys, wrong types, and oversized strings so the server
    # isn't trusting whatever the client decides to send.
    BOOL_KEYS = {
        "is_private", "is_super_private", "anyone_can_post",
        "is_event", "chat_enabled",
    }
    # (key, max_length) — empty strings are stored as NULL.
    STRING_KEYS = {
        "description": 300,
        "event_location": 200,
    }

    if key in BOOL_KEYS:
        if not isinstance(value, bool):
            return Response(
                {"error": f"{key} must be a boolean"}, status=400,
            )
        clean = value

    elif key in STRING_KEYS:
        if value is None:
            clean = None
        elif isinstance(value, str):
            stripped = value.strip()
            if len(stripped) > STRING_KEYS[key]:
                return Response(
                    {
                        "error": (
                            f"{key} must be {STRING_KEYS[key]} characters "
                            f"or fewer"
                        )
                    },
                    status=400,
                )
            clean = stripped if stripped else None
        else:
            return Response(
                {"error": f"{key} must be a string"}, status=400,
            )

    elif key == "event_date":
        # Accept ISO-8601 date strings (YYYY-MM-DD); empty/None clears it.
        if value in (None, ""):
            clean = None
        elif isinstance(value, str):
            from django.utils.dateparse import parse_date
            parsed = parse_date(value)
            if parsed is None:
                return Response(
                    {"error": "event_date must be YYYY-MM-DD"}, status=400,
                )
            clean = parsed
        else:
            return Response(
                {"error": "event_date must be a string"}, status=400,
            )

    elif key == "event_time":
        # Accept HH:MM or HH:MM:SS; empty/None clears it.
        if value in (None, ""):
            clean = None
        elif isinstance(value, str):
            from django.utils.dateparse import parse_time
            parsed = parse_time(value)
            if parsed is None:
                return Response(
                    {"error": "event_time must be HH:MM[:SS]"}, status=400,
                )
            clean = parsed
        else:
            return Response(
                {"error": "event_time must be a string"}, status=400,
            )

    else:
        return Response({"error": "Invalid key"}, status=400)

    setattr(page, key, clean)
    page.save(update_fields=[key])

    return Response({"status": "ok"})
