# Auto-split from the former monolithic api/serializers.py by domain.
# Re-exported from api/serializers/__init__.py so `from api.serializers import X`
# still works. Verified with Django system check + makemigrations --check.

from rest_framework import serializers
from ..models import (
    Page,
)


class BasicPageSerializer(serializers.ModelSerializer):
    avatar = serializers.SerializerMethodField()

    class Meta:
        model = Page
        fields = ['id', 'name', 'avatar', 'is_private']

    def get_avatar(self, obj):
        request = self.context.get('request')
        if obj.avatar:
            return request.build_absolute_uri(obj.avatar.url) if request else obj.avatar.url
        return None

class PageDetailSerializer(serializers.ModelSerializer):
    avatar = serializers.SerializerMethodField()
    is_owner = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()
    has_requested_follow = serializers.SerializerMethodField()
    is_muted = serializers.SerializerMethodField()
    is_in_memories = serializers.SerializerMethodField()
    is_pinned = serializers.SerializerMethodField()
    can_post = serializers.SerializerMethodField()
    followers_count = serializers.SerializerMethodField()

    class Meta:
        model = Page
        fields = [
            'id', 'name', 'description', 'avatar',
            'is_private', 'is_super_private', 'anyone_can_post',
            'is_owner', 'can_post',
            'is_following', 'has_requested_follow', 'followers_count',
            'is_muted', 'is_in_memories', 'is_pinned',
            'is_event', 'event_date', 'event_time', 'event_location',
            'event_address',
            'event_latitude', 'event_longitude', 'event_place_id',
            'chat_enabled',
        ]

    def get_avatar(self, obj):
        request = self.context.get('request')
        if obj.avatar:
            return request.build_absolute_uri(obj.avatar.url) if request else obj.avatar.url
        return None

    def get_is_owner(self, obj):
        return self.context.get('is_owner', False)

    def get_is_following(self, obj):
        return self.context.get('is_following', False)

    def get_has_requested_follow(self, obj):
        return self.context.get('has_requested_follow', False)

    def get_is_muted(self, obj):
        return self.context.get('is_muted', False)

    def get_is_in_memories(self, obj):
        return self.context.get('is_in_memories', False)

    def get_is_pinned(self, obj):
        return self.context.get('is_pinned', False)

    def get_can_post(self, obj):
        return self.context.get('can_post', False)

    def get_followers_count(self, obj):
        return self.context.get('followers_count', 0)
