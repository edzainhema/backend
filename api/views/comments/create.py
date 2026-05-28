"""Creating a comment: mention parsing, hashtags, media validation, and the
notify/push fan-out to the post owner and mentioned users."""


import mimetypes
import re

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


from ...models import BlockedUser, Comment, CommentMention, Notification, Post
from ...serializers import CommentSerializer
from ...services.activity import log_activity
from ...services.push import push_to_user
from ...services.comment_analyzer import analyze_comment, extract_hashtags
from ...services.feed_helpers import viewer_can_see_post
from ...services.media import (
    IMAGE_MAX_BYTES, VIDEO_MAX_BYTES, verify_uploaded_media,
)


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


