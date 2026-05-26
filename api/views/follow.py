import json
import math
import mimetypes
import re

from collections import OrderedDict, defaultdict
from datetime import timedelta
from io import BytesIO
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import IntegrityError, models, transaction
from django.db.models import (
    Case, Count, Exists, F, IntegerField, OuterRef, Prefetch, Q, Value, When,
)
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from PIL import (
    Image, ImageDraw, ImageEnhance, ImageFont, UnidentifiedImageError,
)

from ..models import (
    Activity, BlockedUser, Comment, CommentLike, CommentMention, Conversation,
    ConversationHidden, Device, Follow, FollowRequest, Media,
    Memory, Message, MessageMedia, MessageReaction, MutedPage, MutedUser,
    Notification, Page, PageChatMessage, PageFollow, PageFollowRequest,
    PageInvite, PagePoster, PageReport, Post, PostLike, PostMedia,
    PostMediaTag, PostReport, ProfileVisit, ReelWatch, SavedPost, SearchHistory,
    UserProfile, UserReport, VideoWatch,
)
from ..serializers import (
    BasicPageSerializer, BasicUserSerializer, CommentSerializer,
    ConversationSerializer, FeedPostSerializer, MediaSerializer,
    MessageSerializer, NotificationSerializer, PageDetailSerializer,
    PostMediaSerializer, ProfilePostSerializer, PublicUserProfileSerializer,
    UserProfileSerializer,
)
from ..utils import (
    decode_cursor,
    encode_cursor,
    log_activity,
    push_to_user,
    send_push_notification,
)
from ..comment_analyzer import analyze_comment, extract_hashtags
from ..services.auth_helpers import (
    _find_user_by_identifier, _issue_tokens, _login_or_create_social_user,
    _looks_like_email, _looks_like_phone, _normalize_phone, _username_from_seed,
    _verify_facebook_access_token, _verify_google_id_token,
)
from ..services.feed_helpers import (
    build_feed_context, can_user_post_on_page, get_followed_feed,
    get_friend_ids, get_muted_page_ids, get_social_overlap_score,
    get_social_sets, get_suggested_feed, get_very_close_friend_ids,
    merge_feed, recency_decay, serialize_post,
)
from ..services.media_processing import (
    IMAGE_MAX_BYTES, VIDEO_MAX_BYTES, process_media_image,
    process_media_video, resolve_overlay_font_path, verify_uploaded_media,
    _safe_float, _safe_int, _safe_optional_float,
)
from ..video_filters import VIDEO_FILTER_CHAINS


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_follow(request):
    target_user_id = request.data.get("user_id")

    if not target_user_id:
        return Response(
            {"error": "user_id required"},
            status=400
        )

    # Coerce once, safely: a non-numeric user_id used to reach the bare
    # int(...) below and raise ValueError -> unhandled 500. Mirror the
    # try/except pattern used by not_interested et al. and return 400. The
    # validated int is then reused for the self-check and the lookup.
    try:
        target_user_id = int(target_user_id)
    except (TypeError, ValueError):
        return Response(
            {"error": "invalid user_id"},
            status=400
        )

    if target_user_id == request.user.id:
        return Response(
            {"error": "Cannot follow yourself"},
            status=400
        )

    try:
        target_user = User.objects.get(id=target_user_id)
    except User.DoesNotExist:
        return Response(
            {"error": "User not found"},
            status=404
        )

    # --------------------------------------------------
    # 🚫 BLOCK CHECK (BOTH DIRECTIONS)
    # --------------------------------------------------
    if BlockedUser.objects.between(request.user, target_user).exists():
        return Response(
            {"error": "Not allowed"},
            status=403
        )

    # --------------------------------------------------
    # 🔐 PRIVACY CHECK
    # --------------------------------------------------
    user_profile = getattr(target_user, "userprofile", None)
    is_private = user_profile.is_private if user_profile else False

    # --------------------------------------------------
    # 🔁 UNFOLLOW IF ALREADY FOLLOWING
    # --------------------------------------------------
    existing_follow = Follow.objects.filter(
        follower=request.user,
        following=target_user
    ).first()

    if existing_follow:
        existing_follow.delete()
        # Remove the matching "X started following you" notification so the
        # target doesn't keep seeing a follow that no longer exists. Mirrors
        # the cleanup the cancel-follow-request branch below already does.
        Notification.objects.filter(
            recipient=target_user,
            actor=request.user,
            notification_type="follow",
        ).delete()
        # Invalidate suggested feed — the unfollowed user's posts should
        # now be eligible to reappear as suggestions on the next load.
        cache.delete(f"suggested_feed_scores:{request.user.id}")
        return Response({"status": "unfollowed"})

    # --------------------------------------------------
    # 🔒 PRIVATE ACCOUNT → FOLLOW REQUEST (toggle)
    # --------------------------------------------------
    if is_private:
        existing_request = FollowRequest.objects.filter(
            requester=request.user,
            target=target_user
        ).first()

        # Second tap on "Follow request sent" → cancel the pending request
        if existing_request:
            existing_request.delete()
            # Clean up the matching notification so it doesn't linger
            Notification.objects.filter(
                recipient=target_user,
                actor=request.user,
                notification_type="follow_request"
            ).delete()
            return Response({"status": "request_cancelled"})

        FollowRequest.objects.create(
            requester=request.user,
            target=target_user
        )
        Notification.objects.create(
            recipient=target_user,
            actor=request.user,
            notification_type="follow_request"
        )
        # 🔔 PUSH NOTIFICATION
        push_to_user(
            target_user,
            title="New follow request",
            body=f"{request.user.username} requested to follow you",
            extra_data={"type": "follow_request", "actor_id": request.user.id},
        )

        return Response({"status": "requested"})

    # --------------------------------------------------
    # ✅ PUBLIC ACCOUNT → FOLLOW IMMEDIATELY
    # --------------------------------------------------
    Follow.objects.create(
        follower=request.user,
        following=target_user
    )

    # Invalidate suggested feed — the newly followed user's posts should
    # no longer appear as suggestions on the next load.
    cache.delete(f"suggested_feed_scores:{request.user.id}")

    Notification.objects.create(
        recipient=target_user,
        actor=request.user,
        notification_type="follow"
    )

    # 🔔 PUSH NOTIFICATION
    push_to_user(
        target_user,
        title="New follower",
        body=f"{request.user.username} started following you",
        extra_data={"type": "follow", "actor_id": request.user.id},
    )

    return Response({"status": "following"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def approve_follow_request(request):
    request_id = request.data.get("request_id")

    if not request_id:
        return Response(
            {"error": "request_id required"},
            status=400
        )

    try:
        req = FollowRequest.objects.select_related(
            "requester",
            "target"
        ).get(id=request_id)
    except FollowRequest.DoesNotExist:
        return Response(
            {"error": "Follow request not found"},
            status=404
        )

    # --------------------------------------------------
    # 🔐 MUST BE TARGET USER
    # --------------------------------------------------
    if req.target != request.user:
        return Response(
            {"error": "Not allowed"},
            status=403
        )

    requester = req.requester
    target = req.target  # == request.user

    # --------------------------------------------------
    # 🚫 BLOCK CHECK (BOTH DIRECTIONS)
    # --------------------------------------------------
    if BlockedUser.objects.between(target, requester).exists():
        return Response(
            {"error": "Not allowed"},
            status=403
        )

    # --------------------------------------------------
    # 🔁 CREATE FOLLOW (IF NOT EXISTS)
    # --------------------------------------------------
    Follow.objects.get_or_create(
        follower=requester,
        following=target
    )

    # Invalidate the requester's suggested feed — they now follow `target`
    # so target's posts should leave suggestions.
    cache.delete(f"suggested_feed_scores:{requester.id}")

    # --------------------------------------------------
    # 🧹 CLEAN UP REQUEST
    # --------------------------------------------------
    req.delete()

    # --------------------------------------------------
    # 🔔 NOTIFICATION
    # --------------------------------------------------
    Notification.objects.create(
        recipient=requester,
        actor=target,
        notification_type="follow_approved"
    )

    push_to_user(
        requester,
        title="Follow request approved",
        body=f"{target.username} approved your follow request",
        extra_data={"type": "follow_approved", "actor_id": target.id},
    )

    return Response(
        {"status": "approved"},
        status=200
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reject_follow_request(request):
    requester_id = request.data.get('user_id')

    if not requester_id:
        return Response({"error": "user_id required"}, status=400)

    # Coerce safely: a non-numeric id would otherwise blow up the integer
    # FK filter below with a 500. Mirrors toggle_follow / not_interested.
    try:
        requester_id = int(requester_id)
    except (TypeError, ValueError):
        return Response({"error": "invalid user_id"}, status=400)

    FollowRequest.objects.filter(
        requester_id=requester_id,
        target=request.user
    ).delete()

    # Clean up the matching "follow_request" notification so the rejecting
    # user (its recipient) doesn't keep seeing a request that no longer
    # exists. The notification was created in toggle_follow with
    # recipient=target (== request.user here) and actor=requester. Mirrors
    # the notification cleanup the toggle_follow cancel path already does.
    Notification.objects.filter(
        recipient=request.user,
        actor_id=requester_id,
        notification_type="follow_request",
    ).delete()

    return Response({"status": "rejected"})


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
