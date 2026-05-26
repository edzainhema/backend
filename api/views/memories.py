
from collections import defaultdict

from django.contrib.auth.models import User
from django.db.models import (
    Prefetch, Q,
)
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response



from ..models import (
    BlockedUser, Memory, Page, PageFollow, Post, PostMedia,
)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_page_memory(request):
    page_id = request.data.get("page_id")

    if not page_id:
        return Response(
            {"error": "page_id required"},
            status=400
        )

    page = get_object_or_404(Page, id=page_id)
    user = request.user

    memory = Memory.objects.filter(
        user=user,
        page=page
    ).first()

    if memory:
        # Removing an existing memory is always allowed — the page may have
        # flipped to private after the user originally added it.
        memory.delete()
        return Response({"status": "removed"})

    # ADD branch — mirror the frontend privacy gate on the server so the
    # rule can't be bypassed by hitting the endpoint directly. Private
    # pages can only be added to memories by the owner or an existing
    # follower.
    if page.is_private:
        is_owner = page.owner_id == user.id
        is_following = PageFollow.objects.filter(
            user=user, page=page,
        ).exists()
        if not (is_owner or is_following):
            return Response(
                {"error": "Cannot add a private page you don't follow."},
                status=403,
            )

    Memory.objects.create(
        user=user,
        page=page,
    )
    return Response({"status": "added"})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_user_memories(request):
    viewer = request.user
    user_id = request.query_params.get("user_id")

    if user_id:
        try:
            target_user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=404)
    else:
        target_user = viewer

    # Empty-but-well-formed response used for the two "you can't see
    # this" paths below. Keep the same shape as the happy path so
    # callers don't have to special-case it.
    empty_response = Response(
        {"results": [], "total": 0, "has_more": False},
        status=200,
    )

    if BlockedUser.objects.between(viewer, target_user).exists():
        return empty_response

    target_profile = getattr(target_user, "userprofile", None)
    if target_user != viewer:
        if target_profile and not target_profile.memories_public:
            return empty_response

    # --------------------------------------------------
    # QUERY PARAMS
    # --------------------------------------------------
    search = request.query_params.get("search", "").strip()
    try:
        limit = int(request.query_params.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 30))
    offset = max(0, offset)

    # Hard cap on per-page uploads. The previous version pulled every
    # post that had ever been added to each memory page -- a popular
    # page with thousands of posts would dump them all into a single
    # response. Caps the per-page horizontal scroll list at a sensible
    # size; the client UI scrolls horizontally through it, so 30 covers
    # a few swipes' worth.
    UPLOADS_PER_PAGE = 30

    # --------------------------------------------------
    # VIEWER-VISIBLE PRIVATE PAGES (bulk, not per-memory)
    #
    # The viewer can see a memory iff its page is public, owned by the
    # viewer, or followed by the viewer. The previous version checked
    # this inside the per-memory loop with `PageFollow.objects.filter
    # (...).exists()` -- one query per private memory. Fetching the
    # viewer's full follow + ownership set up front (two small queries)
    # lets us filter invisible memories at the DB layer, so the page
    # boundaries and `total` count are consistent.
    # --------------------------------------------------
    followed_page_ids = set(
        PageFollow.objects
        .filter(user=viewer)
        .values_list("page_id", flat=True)
    )
    owned_page_ids = set(
        Page.objects
        .filter(owner=viewer)
        .values_list("id", flat=True)
    )
    visible_private_page_ids = followed_page_ids | owned_page_ids

    # --------------------------------------------------
    # MEMORIES (paginated slice the viewer can actually see)
    # --------------------------------------------------
    # 🔒 SUPER PRIVATE — Page has two independent booleans, `is_private`
    # and `is_super_private`. The "public" branch below must require BOTH
    # flags to be false; a page with `is_super_private=True` should never
    # leak through, even if it happens to have `is_private=False`. Pages
    # the viewer owns or follows are already covered by the second branch
    # via `visible_private_page_ids` (which we collect for owner/follower
    # status regardless of which privacy flag is set).
    memories_qs = (
        Memory.objects
        .filter(user=target_user)
        .filter(
            Q(page__is_private=False, page__is_super_private=False)
            | Q(page_id__in=visible_private_page_ids)
        )
        .select_related("page")
        .order_by("-created_at")
    )

    if search:
        memories_qs = memories_qs.filter(page__name__icontains=search)

    total = memories_qs.count()
    memories = list(memories_qs[offset:offset + limit])
    page_ids = [m.page_id for m in memories]

    # --------------------------------------------------
    # UPLOADS (single bulk fetch, grouped in Python)
    #
    # The previous version ran a fresh `Post.objects.filter(page=page)`
    # query inside the per-memory loop -- O(memories) round trips. One
    # query for ALL the page ids in the current slice, then group by
    # page_id in Python, is O(1) round trips regardless of slice size.
    #
    # The Python loop also enforces the UPLOADS_PER_PAGE cap so a page
    # with thousands of posts can't blow up the response. We still
    # iterate every row the SQL returned, so for pathologically large
    # memory pages a future improvement would be a window-function
    # `ROW_NUMBER() OVER (PARTITION BY page_id ...)`; for the current
    # data shape the in-Python cap is fine.
    # --------------------------------------------------
    posts_by_page = defaultdict(list)
    if page_ids:
        posts_qs = (
            Post.objects
            .filter(page_id__in=page_ids)
            .select_related("user", "user__userprofile")
            .prefetch_related(
                Prefetch(
                    "media",
                    queryset=PostMedia.objects.order_by("order"),
                    to_attr="ordered_media",
                )
            )
            .order_by("-created_at", "-id")
        )
        for post in posts_qs:
            bucket = posts_by_page[post.page_id]
            if len(bucket) >= UPLOADS_PER_PAGE:
                continue
            bucket.append(post)

    # --------------------------------------------------
    # SERIALIZE
    # --------------------------------------------------
    data = []
    for memory in memories:
        page = memory.page
        uploads = []
        for post in posts_by_page.get(page.id, []):
            if not post.ordered_media:
                continue
            media = post.ordered_media[0]
            uploader = post.user
            uploader_profile = getattr(uploader, "userprofile", None)
            uploads.append({
                "id": media.id,
                "file": request.build_absolute_uri(media.file.url),
                "thumbnail": (
                    request.build_absolute_uri(media.thumbnail.url)
                    if media.thumbnail
                    else None
                ),
                "user": {
                    "id": uploader.id,
                    "username": uploader.username,
                    "avatar": (
                        request.build_absolute_uri(uploader_profile.avatar.url)
                        if uploader_profile and uploader_profile.avatar
                        else None
                    ),
                },
            })

        data.append({
            "page": {
                "id": page.id,
                "name": page.name,
                "avatar": (
                    request.build_absolute_uri(page.avatar.url)
                    if page.avatar else None
                ),
            },
            "uploads": uploads,
        })

    return Response({
        "results": data,
        "total": total,
        "has_more": offset + limit < total,
    })
