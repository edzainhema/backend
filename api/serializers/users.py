# Auto-split from the former monolithic api/serializers.py by domain.
# Re-exported from api/serializers/__init__.py so `from api.serializers import X`
# still works. Verified with Django system check + makemigrations --check.

from rest_framework import serializers
from django.contrib.auth.models import User


class BasicUserSerializer(serializers.ModelSerializer):
    """
    Compact user shape: {id, username, avatar, is_online}.
    Used in feeds, comments, notifications, conversations, etc.
    """
    avatar = serializers.SerializerMethodField()
    # ✅ NEW: online presence indicator
    is_online = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'avatar', 'is_online']

    def get_avatar(self, obj):
        request = self.context.get('request')
        profile = getattr(obj, 'userprofile', None)
        if profile and profile.avatar:
            url = profile.avatar.url
            return request.build_absolute_uri(url) if request else url
        return None

    def get_is_online(self, obj):
        profile = getattr(obj, 'userprofile', None)
        if profile:
            return profile.is_online
        return False

class UserProfileSerializer(serializers.Serializer):
    id = serializers.IntegerField(source='user.id')
    username = serializers.CharField(source='user.username')
    email = serializers.EmailField(source='user.email')

    first_name = serializers.CharField()
    last_name = serializers.CharField()
    phone_number = serializers.CharField()
    bio = serializers.CharField()
    is_private = serializers.BooleanField()
    memories_public = serializers.BooleanField()
    last_username_change = serializers.DateTimeField(allow_null=True)

    avatar = serializers.SerializerMethodField()
    followers_count = serializers.SerializerMethodField()

    def get_avatar(self, obj):
        request = self.context.get('request')
        if obj.avatar:
            return request.build_absolute_uri(obj.avatar.url) if request else obj.avatar.url
        return None

    def get_followers_count(self, obj):
        # Prefer the value passed via context (mirrors PublicUserProfileSerializer)
        # and an optional `followers_count_ann` annotation on the profile object,
        # so views can compute the count once (or annotate) instead of paying for
        # a live .count() on every profile call. Fall back to the live query for
        # callers that haven't been migrated yet.
        ann = getattr(obj, "followers_count_ann", None)
        if ann is not None:
            return ann
        ctx_value = self.context.get("followers_count")
        if ctx_value is not None:
            return ctx_value
        return obj.user.followers.count()

class PublicUserProfileSerializer(serializers.Serializer):
    id = serializers.IntegerField(source='user.id')
    username = serializers.CharField(source='user.username')
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    bio = serializers.CharField(allow_null=True)
    is_private = serializers.BooleanField()
    memories_public = serializers.BooleanField()
    avatar = serializers.SerializerMethodField()
    is_online = serializers.SerializerMethodField()

    is_following = serializers.SerializerMethodField()
    has_requested_follow = serializers.SerializerMethodField()
    followers_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()

    def get_avatar(self, obj):
        request = self.context.get('request')
        if obj.avatar:
            return request.build_absolute_uri(obj.avatar.url) if request else obj.avatar.url
        return None

    def get_is_online(self, obj):
        return obj.is_online

    def get_is_following(self, obj):
        return self.context.get('is_following', False)

    def get_has_requested_follow(self, obj):
        return self.context.get('has_requested_follow', False)

    def get_followers_count(self, obj):
        return self.context.get('followers_count', 0)

    def get_following_count(self, obj):
        return self.context.get('following_count', 0)
