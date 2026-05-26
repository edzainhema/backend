# Auto-split from the former monolithic api/serializers.py by domain.
# Re-exported from api/serializers/__init__.py so `from api.serializers import X`
# still works. Verified with Django system check + makemigrations --check.

from rest_framework import serializers
from ..models import (
    PageInvite,
)
from ..post_media import ordered_media
from .users import BasicUserSerializer


class NotificationSerializer(serializers.Serializer):
    """
    To avoid N+1 queries for follow_request and page_invite notifications,
    pass pre-fetched lookup maps in the serializer context:

      follow_request_map : dict[(actor_id, recipient_id) -> FollowRequest.id]
      page_invite_map    : dict[(actor_id, recipient_id) -> PageInvite.id]

    Build them in the view with two bulk queries before calling the serializer:

      from ..models import FollowRequest, PageInvite
      fr_map = {(fr.requester_id, fr.target_id): fr.id
                for fr in FollowRequest.objects.filter(target=request.user)}
      pi_map = {(pi.invited_by_id, pi.invited_user_id): pi.id
                for pi in PageInvite.objects.filter(invited_user=request.user)}
      context = {'request': request, 'follow_request_map': fr_map, 'page_invite_map': pi_map}
    """
    id             = serializers.IntegerField()
    type           = serializers.CharField(source='notification_type')
    created_at     = serializers.DateTimeField()
    is_read        = serializers.BooleanField()
    actor          = serializers.SerializerMethodField()
    post           = serializers.SerializerMethodField()
    follow_request_id = serializers.SerializerMethodField()
    comment_text   = serializers.SerializerMethodField()
    comment_id     = serializers.IntegerField(source='comment.id', default=None, allow_null=True)
    page_invite_id = serializers.SerializerMethodField()
    invited_page   = serializers.SerializerMethodField()
    page_follow_request_id = serializers.SerializerMethodField()
    notif_page     = serializers.SerializerMethodField()

    def get_actor(self, obj):
        if not obj.actor:
            return None
        return BasicUserSerializer(obj.actor, context=self.context).data

    def get_comment_text(self, obj):
        if obj.notification_type not in ('comment', 'comment_like', 'mention') or not obj.comment_id:
            return None
        if obj.comment.is_deleted:
            return None
        text = obj.comment.text or ''
        return text[:80] + ('…' if len(text) > 80 else '')

    def get_follow_request_id(self, obj):
        if obj.notification_type != "follow_request" or not obj.actor_id:
            return None
        # Prefer the batched map injected by the view (avoids 1 query per notif).
        fr_map = self.context.get("follow_request_map")
        if fr_map is not None:
            return fr_map.get((obj.actor_id, obj.recipient_id))
        from ..models import FollowRequest
        try:
            return FollowRequest.objects.get(requester_id=obj.actor_id, target_id=obj.recipient_id).id
        except FollowRequest.DoesNotExist:
            return None

    def get_page_invite_id(self, obj):
        if obj.notification_type != "page_invite":
            return None
        # Prefer batched map from context (avoids 1 query per notification).
        pi_map = self.context.get("page_invite_map")
        if pi_map is not None:
            return pi_map.get((obj.actor_id, obj.recipient_id))
        from ..models import PageInvite
        try:
            return PageInvite.objects.get(invited_by=obj.actor, invited_user=obj.recipient).id
        except PageInvite.DoesNotExist:
            return None

    def get_invited_page(self, obj):
        if obj.notification_type != "page_invite":
            return None
        request = self.context.get("request")
        page = None
        # 1. Fast path: page FK is now stored on the notification at creation time.
        if obj.page_id:
            page = obj.page
        # 2. Second fast path: pre-fetched map injected by list_notifications view.
        if page is None:
            pi_page_map = self.context.get("page_invite_page_map")
            if pi_page_map is not None:
                page = pi_page_map.get((obj.actor_id, obj.recipient_id))
        # 3. Last resort: single DB query (avoids breaking older notifications without page FK).
        if page is None:
            from ..models import PageInvite
            try:
                page = PageInvite.objects.select_related("page").get(
                    invited_by=obj.actor, invited_user=obj.recipient
                ).page
            except PageInvite.DoesNotExist:
                pass
        if not page:
            return None
        avatar_url = None
        if page.avatar:
            avatar_url = request.build_absolute_uri(page.avatar.url) if request else page.avatar.url
        return {"id": page.id, "name": page.name, "avatar": avatar_url}

    def get_page_follow_request_id(self, obj):
        if obj.notification_type != "page_follow_request":
            return None
        if not obj.page_id or not obj.actor_id:
            return None
        # Prefer the batched map injected by the view (avoids 1 query per notification).
        pfr_map = self.context.get("page_follow_request_map")
        if pfr_map is not None:
            return pfr_map.get((obj.actor_id, obj.page_id))
        from ..models import PageFollowRequest
        try:
            return PageFollowRequest.objects.get(requester_id=obj.actor_id, page_id=obj.page_id).id
        except PageFollowRequest.DoesNotExist:
            return None

    def get_notif_page(self, obj):
        PAGE_TYPES = ("page_follow", "page_follow_request", "page_follow_approved", "page_poster_added")
        if obj.notification_type not in PAGE_TYPES or not obj.page_id:
            return None
        request = self.context.get("request")
        page = obj.page
        avatar_url = None
        if page.avatar:
            avatar_url = request.build_absolute_uri(page.avatar.url) if request else page.avatar.url
        return {"id": page.id, "name": page.name, "avatar": avatar_url}

    def get_post(self, obj):
        post = obj.media
        if not post and obj.notification_type in ('comment', 'comment_like', 'mention', 'comment_reply') and obj.comment_id:
            try:
                post = obj.comment.post
            except Exception:
                pass
        if not post:
            return None
        request = self.context.get('request')

        def abs_url(url):
            return request.build_absolute_uri(url) if request else url

        # Prefer the prefetched `ordered_media` list set by list_notifications
        # (via Prefetch(..., to_attr='ordered_media') -- already sorted by
        # `order` in SQL). Calling `.order_by('order').first()` on the related
        # manager would bypass the prefetch cache and fire a fresh query per
        # notification with a post attached -- a hidden N+1 on what looked like
        # a properly prefetched queryset.
        #
        # Fallback path: if `ordered_media` isn't present (the post came from
        # a code path that doesn't set it), sort the prefetched `media.all()`
        # list in Python -- still cache-friendly, O(n) where n is the per-post
        # media count (typically 1-10).
        ordered = ordered_media(post)
        if not ordered:
            return None
        first_media = ordered[0]

        thumbnail_url = abs_url(first_media.thumbnail.url) if first_media.thumbnail else abs_url(first_media.file.url)
        video_url     = abs_url(first_media.file.url)
        profile       = getattr(post.user, 'userprofile', None)
        avatar_url    = abs_url(profile.avatar.url) if profile and profile.avatar else None
        page_data     = None
        if post.page_id:
            page = post.page
            page_avatar = abs_url(page.avatar.url) if page.avatar else None
            page_data = {'id': page.id, 'name': page.name, 'avatar': page_avatar}

        # Prefer the annotations injected by list_notifications and fall back
        # to a live query only when they're absent. Important: do NOT use
        # `getattr(post, "x", expr)` here — Python evaluates the default
        # expression eagerly, so the fallback `.count()` / `.exists()` queries
        # would fire on every notification even when the annotation is set.
        # Match the rest of the codebase by filtering PostLike/SavedPost via
        # `user=request.user` (the previous `id=request.user.id` was a latent
        # bug — it filtered the like row's primary key against a user id).
        likes_ann    = getattr(post, 'likes_count_ann',    None)
        comments_ann = getattr(post, 'comments_count_ann', None)
        saves_ann    = getattr(post, 'saves_count_ann',    None)
        is_liked_ann = getattr(post, 'is_liked_ann',       None)
        is_saved_ann = getattr(post, 'is_saved_ann',       None)
        viewer       = request.user if request else None

        return {
            'id':             post.id,
            'thumbnail':      thumbnail_url,
            'video':          video_url,
            'description':    post.description or '',
            'created_at':     post.created_at.isoformat(),
            'likes_count':    likes_ann    if likes_ann    is not None else post.likes.count(),
            'comments_count': comments_ann if comments_ann is not None else post.comments.count(),
            'saves_count':    saves_ann    if saves_ann    is not None else post.saved_by.count(),
            'is_liked':       bool(is_liked_ann) if is_liked_ann is not None else (
                post.likes.filter(user=viewer).exists() if viewer else False
            ),
            'is_saved':       bool(is_saved_ann) if is_saved_ann is not None else (
                post.saved_by.filter(user=viewer).exists() if viewer else False
            ),
            'user': {'id': post.user.id, 'username': post.user.username, 'avatar': avatar_url},
            'page': page_data,
        }
