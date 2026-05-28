"""Profile read surface: the signed-in user's own profile, another user's
profile, and the lightweight avatar-only endpoint. `_pinned_pages_payload`
is the shared helper both profile readers use."""


from django.contrib.auth.models import User
from django.db.models import F, Prefetch
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import (
    BlockedUser, Follow, FollowRequest, Page, PageFollow, PinnedPage,
    Post, PostMedia, UserProfile,
)
from ...serializers import (
    ProfilePostSerializer, PublicUserProfileSerializer, UserProfileSerializer,
)
from ...services.feed_helpers import (
    likes_count_subquery, comments_count_subquery, saves_count_subquery,
)


def _pinned_pages_payload(target_user, viewer, request):
    """Pages `target_user` pinned to their profile, as seen by `viewer`.

    Returns a list of {id, name, avatar, is_private}, newest pin first —
    rendered as the row of circular avatars under the Follow / Message
    buttons (Instagram-highlights style).

    Super-private pages are hidden from a viewer who neither owns nor
    follows them (mirrors `list_pages`), so a pin can't leak the existence
    of a secret page. Ordinary private pages are included: their existence
    isn't secret (only their posts are gated), and tapping through still
    hits the Page screen's own privacy checks.
    """
    pins = (
        PinnedPage.objects
        .filter(user=target_user)
        .select_related("page")
        .order_by("-created_at", "-id")
    )

    # Super-private pages this viewer is allowed to see: ones they own or
    # already follow. Computed once so the loop stays query-free.
    visible_super_private_ids = set(
        PageFollow.objects
        .filter(user=viewer)
        .values_list("page_id", flat=True)
    ) | set(
        Page.objects
        .filter(owner=viewer)
        .values_list("id", flat=True)
    )

    out = []
    for pin in pins:
        page = pin.page
        if page.is_super_private and page.id not in visible_super_private_ids:
            continue
        avatar = None
        if page.avatar:
            avatar = request.build_absolute_uri(page.avatar.url)
        out.append({
            "id": page.id,
            "name": page.name,
            "avatar": avatar,
            "is_private": page.is_private,
        })
    return out


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def profile(request):
    user = request.user

    # --------------------------------------------------
    # 📄 PAGINATION
    # The endpoint used to return every post the user had ever made in a
    # single response -- a user with thousands of uploads pulled all of
    # them on every tap of their own profile tab. Mirrors the same
    # limit/offset/sort contract as get_user_profile so the two profile
    # surfaces behave identically and the frontend can share logic.
    # --------------------------------------------------
    sort = request.query_params.get('sort', 'latest')
    if sort not in {"latest", "popular", "oldest"}:
        sort = "latest"

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

    # --------------------------------------------------
    # 🧾 POSTS (paginated slice of the user's uploads)
    # --------------------------------------------------

    base_qs = (
        Post.objects
        .filter(user=user)
        # Named prefetch with pre-ordered media so ProfilePostSerializer
        # can read `obj.ordered_media` directly. Without `to_attr` the
        # serializer chains `.order_by('order')` on the manager, which
        # invalidates the prefetch cache and re-fetches media per post.
        .prefetch_related(
            Prefetch(
                "media",
                queryset=PostMedia.objects.order_by("order"),
                to_attr="ordered_media",
            )
        )
    )

    # Order the *full* queryset in SQL so the chosen sort applies to
    # every upload, not just the slice we're about to serialize.
    # `distinct=True` on each Count is required -- without it the JOINs
    # from prefetched relations inflate the totals.
    if sort == "oldest":
        ordered = base_qs.order_by("created_at", "id")
    elif sort == "popular":
        ordered = (
            base_qs
            .annotate(
                _likes=likes_count_subquery(),
                _comments=comments_count_subquery(),
                _saves=saves_count_subquery(),
            )
            .annotate(
                popularity=F("_likes") + F("_comments") + F("_saves")
            )
            .order_by("-popularity", "-created_at", "-id")
        )
    else:  # "latest"
        ordered = base_qs.order_by("-created_at", "-id")

    # limit+1 trick -- fetch one extra row so we can tell the client
    # whether more pages exist without firing a separate COUNT(*).
    fetched = list(ordered[offset:offset + limit + 1])
    has_more = len(fetched) > limit
    posts = fetched[:limit]

    # --------------------------------------------------
    # 👤 PROFILE DATA (SAFE DEFAULTS)
    # --------------------------------------------------

    profile = getattr(user, "userprofile", None)

    # username cooldown timestamp
    username_last_changed = (
        profile.last_username_change
        if profile and profile.last_username_change
        else None
    )

    # Compute the follower count once and pass it via context. Previously
    # UserProfileSerializer.get_followers_count ran a live .count() on every
    # /auth/profile/ call; centralising it here keeps the serializer pure and
    # lets a future caller skip the query entirely if it already has the value
    # (mirrors the pattern PublicUserProfileSerializer uses).
    followers_count = user.followers.count()

    # --------------------------------------------------
    # 📦 RESPONSE
    # --------------------------------------------------

    ctx = {'request': request, 'followers_count': followers_count}
    posts_data = ProfilePostSerializer(posts, many=True, context=ctx).data

    # Pages the user has pinned to their own profile (viewer == owner here,
    # so every pin is visible to them).
    pinned_pages = _pinned_pages_payload(user, user, request)

    return Response({
        **UserProfileSerializer(profile, context=ctx).data,
        "username_last_changed": username_last_changed,
        "posts": posts_data,
        "has_more": has_more,
        "pinned_pages": pinned_pages,
    })


# Lightweight endpoint for surfaces that need the viewer's avatar URL only
# (e.g. the Comments composer). The full /auth/profile/ endpoint serializes
# the first page of the viewer's posts, runs a follower count, and walks the
# full UserProfileSerializer — way too heavy when the caller just wants a
# 32px avatar. This endpoint is a single indexed FK hop on UserProfile.
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_avatar(request):
    profile = (
        UserProfile.objects
        .only("id", "user_id", "avatar")
        .filter(user_id=request.user.id)
        .first()
    )
    avatar_url = None
    if profile and profile.avatar:
        url = profile.avatar.url
        avatar_url = request.build_absolute_uri(url)
    return Response({"avatar": avatar_url})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_user_profile(request):
    viewer = request.user
    user_id = request.query_params.get("user_id")
    if not user_id:
        return Response(
            {"error": "user_id is required"},
            status=400
        )

    # Sort order for the uploads grid. Anything outside the known set falls
    # through to "latest" so the API stays well-defined for old clients.
    sort = request.query_params.get('sort', 'latest')
    if sort not in {"latest", "popular", "oldest"}:
        sort = "latest"

    # Pagination for the uploads grid. Same shape as get_page_detail —
    # default page size matches the 3x3 grid; values are clamped so a
    # client can't ask for an unbounded slice.
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
    try:
        target_user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response(
            {"error": "User not found"},
            status=404
        )
    # --------------------------------------------------
    # 🚫 BLOCK CHECK (both directions)
    # --------------------------------------------------
    is_blocked = BlockedUser.objects.between(viewer, target_user).exists()
    if is_blocked:
        return Response(
            {"error": "User not found"},
            status=404
        )
    # --------------------------------------------------
    # 👤 PROFILE + FOLLOW STATE
    # --------------------------------------------------
    is_own_profile = viewer.id == target_user.id
    is_following = Follow.objects.filter(
        follower=viewer,
        following=target_user
    ).exists()
    followers_count = Follow.objects.filter(
        following=target_user
    ).count()
    following_count = Follow.objects.filter(
        follower=target_user
    ).count()
    # Older accounts (or ones where the post-save signal failed) may not
    # have a UserProfile row yet. Auto-create one with defaults so the
    # serializer below has a real instance to walk — otherwise it crashes
    # trying to read `.user.id`, `.is_private`, etc. off `None`.
    user_profile, _ = UserProfile.objects.get_or_create(user=target_user)
    is_private = user_profile.is_private
    has_requested_follow = (
        not is_own_profile
        and is_private
        and not is_following
        and FollowRequest.objects.filter(
            requester=viewer,
            target=target_user
        ).exists()
    )
    # --------------------------------------------------
    # 🔐 VISIBILITY RULES
    # --------------------------------------------------
    can_view_posts = (
        is_own_profile or
        not is_private or
        is_following
    )
    # --------------------------------------------------
    # 🧾 POSTS (only if allowed)
    # --------------------------------------------------
    ctx = {'request': request}
    has_more = False
    if can_view_posts:
        base_qs = (
            Post.objects
            .filter(user=target_user)
            .select_related("page")          # ← only change
            # Named prefetch with pre-ordered media — see the same fix
            # in `profile()` above. Eliminates the N+1 inside
            # ProfilePostSerializer.get_media.
            .prefetch_related(
                Prefetch(
                    "media",
                    queryset=PostMedia.objects.order_by("order"),
                    to_attr="ordered_media",
                )
            )
        )

        # Order the *full* queryset in SQL so the chosen sort applies to
        # every upload, not just the slice we're about to serialize.
        # `distinct=True` on each Count is required — without it the JOINs
        # from prefetched relations inflate the totals.
        if sort == "oldest":
            ordered = base_qs.order_by("created_at", "id")
        elif sort == "popular":
            ordered = (
                base_qs
                .annotate(
                    _likes=likes_count_subquery(),
                    _comments=comments_count_subquery(),
                    _saves=saves_count_subquery(),
                )
                .annotate(
                    popularity=F("_likes") + F("_comments") + F("_saves")
                )
                .order_by("-popularity", "-created_at", "-id")
            )
        else:  # "latest"
            ordered = base_qs.order_by("-created_at", "-id")

        # Fetch limit+1 to detect `has_more` without a separate COUNT(*).
        fetched = list(ordered[offset:offset + limit + 1])
        has_more = len(fetched) > limit
        posts = fetched[:limit]

        posts_data = ProfilePostSerializer(posts, many=True, context=ctx).data
    else:
        posts_data = []
    # --------------------------------------------------
    # 📌 PINNED PAGES
    # The pages this user chose to showcase, rendered under the
    # Follow / Message buttons. Always returned (independent of the
    # posts-visibility gate) — pinned pages are a public showcase, like
    # Instagram highlights, and a private account can still show them.
    # --------------------------------------------------
    pinned_pages = _pinned_pages_payload(target_user, viewer, request)

    # --------------------------------------------------
    # 📦 RESPONSE
    # --------------------------------------------------
    return Response({
        **PublicUserProfileSerializer(
            user_profile,
            context={
                **ctx,
                'is_following': is_following,
                'has_requested_follow': has_requested_follow,
                'followers_count': followers_count,
                'following_count': following_count,
            },
        ).data,
        "posts": posts_data,
        "has_more": has_more,
        "pinned_pages": pinned_pages,
    })


