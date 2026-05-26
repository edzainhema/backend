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
from django.db.models.functions import Lower
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
from ..utils import log_activity, push_to_user, send_push_notification
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
    merge_feed, recency_decay, serialize_post, viewer_can_see_post,
)
from ..services.media_processing import (
    IMAGE_MAX_BYTES, VIDEO_MAX_BYTES, process_media_image,
    process_media_video, resolve_overlay_font_path, verify_uploaded_media,
    _safe_float, _safe_int, _safe_optional_float,
)
from ..video_filters import VIDEO_FILTER_CHAINS


COMMENTS_PAGE_DEFAULT = 20
COMMENTS_PAGE_MAX = 50

# Throttle window for comment-like push notifications. Within this many
# seconds of having already pushed for (actor, comment), we suppress
# follow-up pushes — the in-app notification is still kept up-to-date,
# but the recipient's lock screen doesn't get spammed by like/unlike
# flip-flopping. 30s comfortably covers the rapid-tap case without
# silencing genuinely separate engagements.
COMMENT_LIKE_PUSH_THROTTLE_SECONDS = 30


# `viewer_can_see_post` — the single-post visibility gate — now lives in
# services.feed_helpers (imported above), next to its queryset twin
# `post_visibility_q`, so the comment AND post-engagement endpoints share one
# implementation instead of each carrying their own copy.


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_comments(request):
    post_id = request.query_params.get("post_id")

    if not post_id:
        return Response(
            {"error": "post_id required"},
            status=400
        )

    # Pagination: cap top-level comments per request so a viral post can't
    # force the endpoint to serialize thousands of rows + their replies in
    # one response. Clients page in by passing ?offset=N&limit=M; clients
    # that omit these get the default page (preserves backwards-compatible
    # behaviour for any callers that haven't been updated yet, just with a
    # bounded payload).
    try:
        limit = int(request.query_params.get("limit", COMMENTS_PAGE_DEFAULT))
    except (TypeError, ValueError):
        limit = COMMENTS_PAGE_DEFAULT
    limit = max(1, min(limit, COMMENTS_PAGE_MAX))

    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    post = get_object_or_404(
        Post.objects.select_related("user", "user__userprofile", "page"),
        id=post_id,
    )

    # Visibility check: previously this endpoint only filtered comments by
    # block status, but happily returned the comment list for a post the
    # viewer couldn't otherwise see (private account they don't follow,
    # private page they don't follow, etc.). Return 404 — not 403 — so we
    # don't leak the existence of the post to a viewer who shouldn't know
    # about it.
    if not viewer_can_see_post(request.user, post):
        return Response({"error": "Not found"}, status=404)

    blocked_pairs = BlockedUser.objects.involving(request.user).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_user_ids = set()
    for u, b in blocked_pairs:
        blocked_user_ids.add(u)
        blocked_user_ids.add(b)

    blocked_user_ids.discard(request.user.id)

    # Annotate likes_count + is_liked at the SQL layer so the database returns
    # an integer and a boolean per comment, rather than handing back every
    # CommentLike row for Python to len()/scan. This is what keeps the endpoint
    # cheap when a single comment gets thousands of likes — the wire and memory
    # cost is now O(1) per comment instead of O(likes).
    #
    # The likes_count_ann uses a filtered Count so likes from users the viewer
    # has blocked (or who have blocked the viewer) don't inflate the visible
    # count. Otherwise the displayed like number could disagree with the set
    # of comments the viewer can actually see.
    viewer_liked = CommentLike.objects.filter(
        comment=OuterRef("pk"),
        user=request.user,
    )

    likes_count_expr = Count(
        "likes",
        filter=~Q(likes__user_id__in=blocked_user_ids),
    )

    # Build the reply queryset once, with the same block-filter, ordering, and
    # annotations the top-level comments use. Attaching it via Prefetch
    # (to_attr="filtered_replies") keeps the cache intact — calling
    # c.replies.exclude(...) on a related manager would otherwise bust the
    # prefetch and issue a fresh query per top-level comment.
    reply_qs = (
        Comment.objects
        .exclude(user_id__in=blocked_user_ids)
        .select_related("user", "user__userprofile")
        .annotate(
            likes_count_ann=likes_count_expr,
            is_liked_ann=Exists(viewer_liked),
        )
        .order_by("created_at")
    )

    base = (
        Comment.objects
        .filter(
            post=post,
            parent__isnull=True
        )
        .exclude(user_id__in=blocked_user_ids)
        .select_related("user", "user__userprofile")
        .annotate(
            likes_count_ann=likes_count_expr,
            is_liked_ann=Exists(viewer_liked),
        )
        .prefetch_related(
            Prefetch("replies", queryset=reply_qs, to_attr="filtered_replies"),
        )
        .order_by("created_at")
    )

    # Fetch one extra row to learn whether another page exists without
    # paying for a separate COUNT(*) (which would scan the whole table for
    # popular posts). If we got back limit+1 rows, more pages remain; drop
    # the extra before serializing.
    page = list(base[offset:offset + limit + 1])
    has_more = len(page) > limit
    page = page[:limit]

    ctx = {'request': request, 'viewer': request.user}
    data = []

    for c in page:
        # filtered_replies is the cached, pre-filtered list populated by the
        # Prefetch above — zero extra queries per top-level comment.
        replies = c.filtered_replies
        comment_data = CommentSerializer(c, context=ctx).data
        comment_data['replies'] = CommentSerializer(
            replies, many=True, context=ctx
        ).data
        data.append(comment_data)

    return Response({
        "comments": data,
        "has_more": has_more,
        "next_offset": offset + len(data) if has_more else None,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_comment(request):
    post_id = request.data.get("post_id")
    parent_id = request.data.get("parent_id")
    text = request.data.get("text", "").strip()
    file = request.FILES.get("file")

    if not post_id:
        return Response(
            {"error": "post_id required"},
            status=400
        )

    if not text and not file:
        return Response(
            {"error": "Comment must have text or file"},
            status=400
        )

    post = get_object_or_404(
        Post.objects.select_related("user", "user__userprofile", "page"),
        id=post_id,
    )
    post_owner = post.user

    # Visibility gate (mirrors get_comments): you may only comment on a post
    # you're allowed to see. viewer_can_see_post also subsumes the block check
    # against the post author. Return 404 — not 403 — so the endpoint can't be
    # used to confirm that a post the viewer can't otherwise see exists.
    if not viewer_can_see_post(request.user, post):
        return Response({"error": "Not found"}, status=404)

    parent = None
    if parent_id:
        parent = get_object_or_404(
            Comment,
            id=parent_id,
            post=post
        )

        if BlockedUser.objects.between(request.user, parent.user).exists():
            return Response(
                {"error": "Not allowed"},
                status=403
            )

        # Flatten reply chains: the listing endpoint only surfaces top-level
        # comments + one level of replies, so a reply attached to another
        # reply would be saved but invisible. Walk up to the top-level
        # ancestor and attach there instead. The block check above stays on
        # the comment the user actually clicked "reply" on.
        while parent.parent_id is not None:
            parent = parent.parent

    # File validation: content-type, size, magic bytes. Mirrors posts.py.
    if file:
        client_ct = (file.content_type or '').lower()
        guessed_ct = (mimetypes.guess_type(file.name or '')[0] or '').lower()
        is_image_ct = client_ct.startswith('image/')
        is_video_ct = client_ct.startswith('video/')

        if not (is_image_ct or is_video_ct):
            return Response(
                {'error': f'Unsupported file type: {client_ct or "unknown"}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if guessed_ct and not guessed_ct.startswith(client_ct.split('/')[0]):
            return Response(
                {'error': f'Content-type does not match filename: {file.name}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        size_cap = IMAGE_MAX_BYTES if is_image_ct else VIDEO_MAX_BYTES
        if file.size is not None and file.size > size_cap:
            limit_mb = size_cap // (1024 * 1024)
            kind_label = 'Image' if is_image_ct else 'Video'
            return Response(
                {'error': f'{kind_label} exceeds the {limit_mb} MB limit.'},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        try:
            verify_uploaded_media(
                file,
                claimed_kind='image' if is_image_ct else 'video',
            )
        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # Wrap comment + side effects in a single transaction. Roll back if any
    # step raises so we never persist a half-saved comment.
    with transaction.atomic():
        comment = Comment.objects.create(
            post=post,
            user=request.user,
            parent=parent,
            text=text,
            file=file
        )

        sentiment_label, sentiment_score, kw_tags = analyze_comment(text or "")
        log_activity(
            request.user,
            "post_comment",
            post=post,
            comment=comment,
            sentiment_label=sentiment_label,
            sentiment_score=sentiment_score,
            keywords=kw_tags,
        )
        for tag in extract_hashtags(text or ""):
            log_activity(
                request.user,
                "hashtag_engage",
                post=post,
                comment=comment,
                hashtag=tag,
            )

        # Dedupe notifications. The post owner might also be the parent
        # commenter; either might also be @mentioned. Previously each path
        # fired its own Notification + push, so a single comment could
        # buzz the same person two or three times. Track who has already
        # been notified for this comment and pick the most-specific event
        # type for them — comment_reply > mention > comment.
        notified_recipient_ids = {request.user.id}

        def _notify(recipient, ntype, title, body, extra_data):
            if recipient is None:
                return
            if recipient.id in notified_recipient_ids:
                return
            notified_recipient_ids.add(recipient.id)
            Notification.objects.create(
                recipient=recipient,
                actor=request.user,
                notification_type=ntype,
                media=post,
                comment=comment,
            )
            push_to_user(
                recipient,
                title=title,
                body=body,
                extra_data=extra_data,
            )

        # Reply > mention > comment, so notify the parent commenter first
        # (most-specific event), then the post owner, then mentions.
        if parent and parent.user_id != request.user.id:
            _notify(
                parent.user,
                "comment_reply",
                title="New reply",
                body=f"{request.user.username} replied to your comment",
                extra_data={
                    "type": "comment_reply",
                    "post_id": post.id,
                    "comment_id": comment.id,
                    "parent_comment_id": parent.id,
                    "actor_id": request.user.id,
                },
            )

        _notify(
            post_owner,
            "comment",
            title="New comment",
            body=f"{request.user.username} commented on your post",
            extra_data={
                "type": "comment",
                "post_id": post.id,
                "comment_id": comment.id,
                "actor_id": request.user.id,
            },
        )

        # Mentions. Case-insensitive lookup — the regex captures the literal
        # text the user typed, but usernames are stored case-as-registered,
        # so @Bob should resolve to user 'bob'. Lower("username") gives a
        # functional index lookup without per-user query gymnastics.
        mentioned_usernames = set(
            re.findall(r"@([A-Za-z0-9_]{1,30})", text)
        )

        if mentioned_usernames:
            lowered_usernames = {u.lower() for u in mentioned_usernames}
            mentioned_users = User.objects.annotate(
                lower_username=Lower("username"),
            ).filter(lower_username__in=lowered_usernames)

            for u in mentioned_users:
                if BlockedUser.objects.between(request.user, u).exists():
                    continue

                CommentMention.objects.get_or_create(
                    comment=comment,
                    mentioned_user=u
                )

                _notify(
                    u,
                    "mention",
                    title="You were mentioned",
                    body=f"{request.user.username} mentioned you",
                    extra_data={
                        "type": "comment_mention",
                        "post_id": post.id,
                        "comment_id": comment.id,
                        "actor_id": request.user.id,
                    },
                )

    ctx = {'request': request, 'viewer': request.user}
    return Response(
        {
            **CommentSerializer(comment, context=ctx).data,
            "post_id": comment.post_id,
            "parent_id": comment.parent_id,
        },
        status=201,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delete_comment(request):
    comment_id = request.data.get("comment_id")
    if not comment_id:
        return Response({"error": "comment_id required"}, status=400)

    comment = get_object_or_404(Comment, id=comment_id)

    if comment.user != request.user:
        return Response({"error": "Not allowed"}, status=403)

    # Hard-delete the attached blob from storage before clearing the field.
    # Soft-deleting only the DB row leaves the underlying image/video sitting
    # in the media bucket forever, which adds up to real money for any
    # popular post. The save=False keeps this in our single .save() below.
    if comment.file:
        try:
            comment.file.delete(save=False)
        except Exception:
            # Don't fail the soft-delete if storage hiccups; a background
            # sweeper can still pick up the orphan from the (now NULL) ref.
            pass

    comment.is_deleted = True
    comment.deleted_at = timezone.now()
    comment.text = ""
    comment.file = None
    comment.save(update_fields=["is_deleted", "deleted_at", "text", "file"])

    return Response({"status": "deleted"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_comment_like(request):
    comment_id = request.data.get("comment_id")

    if not comment_id:
        return Response(
            {"error": "comment_id required"},
            status=400
        )

    comment = get_object_or_404(
        Comment.objects.select_related(
            "post", "post__user", "post__user__userprofile", "post__page",
            "user",
        ),
        id=comment_id,
    )
    comment_owner = comment.user

    # Must be allowed to see the underlying post (same gate as the comments
    # list / create) — otherwise comment ids could be probed/liked on posts
    # the viewer can't see. viewer_can_see_post returns 404 (existence-hiding);
    # the separate check below still 403s when the COMMENT's author (who may
    # differ from the post author) has blocked the viewer.
    if not viewer_can_see_post(request.user, comment.post):
        return Response({"error": "Not found"}, status=404)

    if BlockedUser.objects.between(request.user, comment_owner).exists():
        return Response(
            {"error": "Not allowed"},
            status=403
        )

    like, created = CommentLike.objects.get_or_create(
        user=request.user,
        comment=comment
    )

    if not created:
        like.delete()
        # Remove any prior "X liked your comment" notification for this
        # actor/comment so the recipient's feed doesn't keep a phantom
        # like notification after the user changes their mind.
        Notification.objects.filter(
            recipient=comment_owner,
            actor=request.user,
            notification_type="comment_like",
            comment=comment,
        ).delete()
        return Response({"liked": False})

    if comment_owner != request.user:
        Notification.objects.create(
            recipient=comment_owner,
            actor=request.user,
            notification_type="comment_like",
            media=comment.post,
            comment=comment,
        )
        # Throttle the push (not the in-app notification). The in-app
        # notification is created/deleted in lockstep with the like row,
        # so it'll already accurately reflect the latest state. The push,
        # by contrast, can't be unsent once it's gone to APNs/FCM — and
        # a spammy like/unlike sender otherwise floods the recipient's
        # lock screen even though their in-app history shows nothing.
        # cache.add() is atomic: it sets the key only if absent and
        # returns True when it wins the race, so we push exactly once per
        # (actor, comment) per throttle window.
        push_key = f"clike_push:{request.user.id}:{comment.id}"
        if cache.add(push_key, True, timeout=COMMENT_LIKE_PUSH_THROTTLE_SECONDS):
            push_to_user(
                comment_owner,
                title="New like",
                body=f"{request.user.username} liked your comment",
                extra_data={
                    "type": "comment_like",
                    "post_id": comment.post.id,
                    "comment_id": comment.id,
                    "actor_id": request.user.id,
                },
            )

    return Response({"liked": True})
