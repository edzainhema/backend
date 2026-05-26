# Auto-split from the former monolithic api/serializers.py by domain.
# Re-exported from api/serializers/__init__.py so `from api.serializers import X`
# still works. Verified with Django system check + makemigrations --check.

from rest_framework import serializers
from ..models import (
    Media,
)
from ..post_media import ordered_media
from .users import BasicUserSerializer
from .pages import BasicPageSerializer


class MediaSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = Media
        fields = ['id', 'user', 'file', 'file_url', 'uploaded_at']

    def get_file_url(self, obj):
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(obj.file.url)
        return obj.file.url

class PostMediaSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    file = serializers.SerializerMethodField()
    thumbnail = serializers.SerializerMethodField()
    order = serializers.IntegerField()
    tags = serializers.SerializerMethodField()
    # width/height come straight off the model (captured at upload time).
    # IntegerField with allow_null lets legacy rows that pre-date these
    # columns serialize as null — the client falls back to runtime sizing.
    width = serializers.IntegerField(allow_null=True)
    height = serializers.IntegerField(allow_null=True)

    def get_file(self, obj):
        request = self.context.get('request')
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url

    def get_thumbnail(self, obj):
        request = self.context.get('request')
        if obj.thumbnail:
            return request.build_absolute_uri(obj.thumbnail.url) if request else obj.thumbnail.url
        return None

    def get_tags(self, obj):
        return [
            {'id': t.user.id, 'username': t.user.username}
            for t in obj.tags.all()
        ]

class ProfilePostMediaSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    file = serializers.SerializerMethodField()
    thumbnail = serializers.SerializerMethodField()
    order = serializers.IntegerField()
    # See PostMediaSerializer for rationale on the nullable width/height.
    width = serializers.IntegerField(allow_null=True)
    height = serializers.IntegerField(allow_null=True)

    def get_file(self, obj):
        request = self.context.get('request')
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url

    def get_thumbnail(self, obj):
        request = self.context.get('request')
        if obj.thumbnail:
            return request.build_absolute_uri(obj.thumbnail.url) if request else obj.thumbnail.url
        return None

class ProfilePostSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    description = serializers.CharField()
    created_at = serializers.DateTimeField()
    media = serializers.SerializerMethodField()
    page = serializers.SerializerMethodField()

    def get_media(self, obj):
        # Prefer the prefetched-and-pre-ordered list when the view set one
        # up via `Prefetch("media", queryset=..., to_attr="ordered_media")`.
        # Falling back to `media.all().order_by('order')` here keeps the
        # serializer backward-compatible with any caller that hasn't been
        # updated to the named prefetch — but those callers will fire one
        # extra query per post (N+1) until they are.
        ordered = ordered_media(obj)
        return ProfilePostMediaSerializer(
            ordered,
            many=True,
            context=self.context,
        ).data

    def get_page(self, obj):
        if not obj.page:
            return None
        request = self.context.get('request')
        avatar = None
        if obj.page.avatar:
            avatar = request.build_absolute_uri(obj.page.avatar.url)
        return {"id": obj.page.id, "name": obj.page.name, "avatar": avatar}

class FeedPostSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    description = serializers.CharField()
    created_at = serializers.DateTimeField()
    suggested = serializers.SerializerMethodField()

    user = serializers.SerializerMethodField()
    page = serializers.SerializerMethodField()

    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    is_owner = serializers.SerializerMethodField()

    comments_count = serializers.SerializerMethodField()

    saves_count = serializers.SerializerMethodField()
    is_saved = serializers.SerializerMethodField()

    is_followed = serializers.SerializerMethodField()
    is_page_followed = serializers.SerializerMethodField()

    is_public_override = serializers.SerializerMethodField()

    top_comments = serializers.SerializerMethodField()
    media = serializers.SerializerMethodField()

    def _viewer(self):
        return self.context.get('viewer')

    def get_suggested(self, obj):
        return self.context.get('suggested', False)

    def get_user(self, obj):
        return BasicUserSerializer(obj.user, context=self.context).data

    def get_page(self, obj):
        if obj.page:
            return BasicPageSerializer(obj.page, context=self.context).data
        return None

    def get_likes_count(self, obj):
        # Prefer annotation set by the feed queryset to avoid an extra DB hit.
        ann = getattr(obj, "likes_count_ann", None)
        return ann if ann is not None else obj.likes.count()

    def get_is_liked(self, obj):
        viewer_liked = getattr(obj, "viewer_liked", None)
        if viewer_liked is not None:
            return bool(viewer_liked)
        viewer = self._viewer()
        return obj.likes.filter(user=viewer).exists() if viewer else False

    def get_is_owner(self, obj):
        viewer = self._viewer()
        return obj.user_id == viewer.id if viewer else False

    def get_comments_count(self, obj):
        ann = getattr(obj, "comments_count_ann", None)
        return ann if ann is not None else obj.comments.count()

    def get_saves_count(self, obj):
        ann = getattr(obj, "saves_count_ann", None)
        return ann if ann is not None else obj.saved_by.count()

    def get_is_saved(self, obj):
        viewer_saved = getattr(obj, "viewer_saved", None)
        if viewer_saved is not None:
            return bool(viewer_saved)
        viewer = self._viewer()
        return obj.saved_by.filter(user=viewer).exists() if viewer else False

    def get_is_followed(self, obj):
        followed_users = self.context.get('followed_users')
        if followed_users is not None:
            return obj.user_id in followed_users
        from ..models import Follow
        viewer = self._viewer()
        return Follow.objects.filter(follower=viewer, following=obj.user).exists() if viewer else False

    def get_is_page_followed(self, obj):
        if not obj.page:
            return False
        followed_pages = self.context.get('followed_pages')
        if followed_pages is not None:
            return obj.page_id in followed_pages
        from ..models import PageFollow
        viewer = self._viewer()
        return PageFollow.objects.filter(user=viewer, page=obj.page).exists() if viewer else False

    def get_is_public_override(self, obj):
        return obj.is_public_override

    def get_top_comments(self, obj):
        return self.context.get('top_comments', {}).get(obj.id, [])

    def get_media(self, obj):
        # Prefer `ordered_media` (a Python list populated by a Prefetch with
        # to_attr=...) so we don't bust the prefetch cache. Calling
        # `obj.media.all().order_by('order')` on the related manager would
        # invalidate `prefetch_related("media")` and re-query per post, which
        # is the single biggest source of waste on the home feed.
        # Fallback: sort the prefetched list in Python (no DB hit, O(n) with
        # n = per-post media count, typically <= 10).
        ordered = ordered_media(obj)
        return PostMediaSerializer(
            ordered,
            many=True,
            context=self.context,
        ).data

class CommentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    text = serializers.SerializerMethodField()
    file = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    is_deleted = serializers.BooleanField()
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    user = serializers.SerializerMethodField()

    def get_text(self, obj):
        return '' if obj.is_deleted else obj.text

    def get_file(self, obj):
        request = self.context.get('request')
        if obj.file:
            return request.build_absolute_uri(obj.file.url) if request else obj.file.url
        return None

    def get_likes_count(self, obj):
        # Prefer the SQL-side annotation injected by get_comments
        # (Count("likes") exposed as likes_count_ann). Falls back to a live
        # COUNT for callers that hand in a single un-annotated comment
        # (e.g. create_comment, where the count is 0 anyway).
        # NOTE: do not use getattr(obj, "attr", default_expr) — Python evaluates
        # default_expr eagerly, which would re-introduce a per-comment query.
        ann = getattr(obj, "likes_count_ann", None)
        if ann is not None:
            return ann
        return obj.likes.count()

    def get_is_liked(self, obj):
        viewer = self.context.get('viewer')
        if not viewer:
            return False
        # Prefer the SQL-side Exists annotation (is_liked_ann). Same fallback
        # rules as get_likes_count.
        ann = getattr(obj, "is_liked_ann", None)
        if ann is not None:
            return bool(ann)
        return obj.likes.filter(user=viewer).exists()

    def get_user(self, obj):
        return BasicUserSerializer(obj.user, context=self.context).data
