# Auto-split from the former monolithic api/serializers.py by domain.
# Re-exported from api/serializers/__init__.py so `from api.serializers import X`
# still works. Verified with Django system check + makemigrations --check.

from rest_framework import serializers
from django.contrib.auth.models import User
from ..models import (
    Message,
    MessageMedia,
    Conversation,
)
from .users import BasicUserSerializer


class MessageSerializer(serializers.ModelSerializer):
    sender        = serializers.CharField(source='sender.username')
    sender_avatar = serializers.SerializerMethodField()
    text          = serializers.SerializerMethodField()
    read_by       = serializers.SerializerMethodField()
    is_mine       = serializers.SerializerMethodField()
    media_url     = serializers.SerializerMethodField()
    reactions     = serializers.SerializerMethodField()
    reply_to      = serializers.SerializerMethodField()
    media_items   = serializers.SerializerMethodField()
    # ✅ NEW: edit fields
    is_edited     = serializers.BooleanField()
    last_edited_at = serializers.DateTimeField(allow_null=True)

    class Meta:
        model = Message
        fields = [
            'id', 'sender_id', 'sender', 'sender_avatar',
            'text', 'created_at', 'is_deleted',
            'is_edited', 'last_edited_at',
            'read_by', 'is_mine',
            'media_url', 'media_type',
            'media_items',
            'reactions',
            'reply_to',
        ]

    def get_sender_avatar(self, obj):
        request = self.context.get('request')
        profile = getattr(obj.sender, 'userprofile', None)
        if profile and profile.avatar:
            url = profile.avatar.url
            return request.build_absolute_uri(url) if request else url
        return None

    def get_text(self, obj):
        return '' if obj.is_deleted else obj.text

    def get_read_by(self, obj):
        return [u.id for u in obj.read_by.all()]

    def get_is_mine(self, obj):
        viewer = self.context.get('viewer')
        return obj.sender_id == viewer.id if viewer else False

    def get_media_url(self, obj):
        if obj.is_deleted or not obj.media:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.media.url) if request else obj.media.url

    def get_media_items(self, obj):
        if obj.is_deleted:
            return []
        request = self.context.get('request')
        items = []
        for item in obj.media_items.all():
            url = request.build_absolute_uri(item.file.url) if request else item.file.url
            items.append({"url": url, "media_type": item.media_type})
        return items

    def get_reactions(self, obj):
        result: dict = {}
        for reaction in obj.reactions.all():
            result[reaction.emoji] = result.get(reaction.emoji, 0) + 1
        return result

    def get_reply_to(self, obj):
        r = obj.reply_to
        if not r:
            return None
        request = self.context.get('request')
        media_url = None
        if not r.is_deleted and r.media:
            media_url = request.build_absolute_uri(r.media.url) if request else r.media.url
        first_item = r.media_items.first() if not r.is_deleted else None
        if not media_url and first_item:
            media_url = request.build_absolute_uri(first_item.file.url) if request else first_item.file.url
        reply_media_type = r.media_type or (first_item.media_type if first_item else None)
        return {
            "id":         r.id,
            "sender_id":  r.sender_id,
            "sender":     r.sender.username,
            "text":       "" if r.is_deleted else r.text,
            "media_url":  media_url,
            "media_type": reply_media_type,
            "is_deleted": r.is_deleted,
        }

class ConversationSerializer(serializers.Serializer):
    """
    Conversation list item.

    Expected context (all pre-computed by `list_conversations` in views.py):
      - viewer:           the requesting User
      - last_msg_map:     { conversation_id: latest Message instance }
      - legacy_media_map: { message_id: media_type } -- for latest messages
                          whose preview must come from MessageMedia rather
                          than the legacy `media_type` column
      - unread_map:       { conversation_id: int }

    Each method has a safe fallback path so the serializer still works
    when invoked outside of `list_conversations` (e.g. ad-hoc usage),
    just at a higher query cost.
    """
    conversation_id = serializers.IntegerField(source='id')
    name            = serializers.CharField()
    participants    = serializers.SerializerMethodField()
    last_message    = serializers.SerializerMethodField()
    timestamp       = serializers.SerializerMethodField()
    avatar_user     = serializers.SerializerMethodField()
    unread_count    = serializers.SerializerMethodField()

    # -- Helpers ----------------------------------------------------------

    def _participants_excluding_viewer(self, obj):
        """Use the prefetched participants list when present (no extra query)."""
        viewer = self.context.get('viewer')
        if viewer is None:
            return list(obj.participants.all())
        return [p for p in obj.participants.all() if p.id != viewer.id]

    def _get_last_message(self, obj):
        cached_map = self.context.get('last_msg_map')
        if cached_map is not None:
            return cached_map.get(obj.id)
        # Fallback -- not used by list_conversations, but keeps the
        # serializer correct in standalone usage.
        return (
            obj.messages
            .select_related('sender__userprofile')
            .order_by('-created_at')
            .first()
        )

    # -- Fields -----------------------------------------------------------

    def get_name(self, obj):
        return obj.name or ""

    def get_participants(self, obj):
        return BasicUserSerializer(
            self._participants_excluding_viewer(obj),
            many=True,
            context=self.context,
        ).data

    def get_last_message(self, obj):
        last = self._get_last_message(obj)
        if not last:
            return ''
        if last.is_deleted:
            return '\U0001F6AB Message deleted'
        if last.text:
            return last.text
        if last.media_type == 'image':
            return '\U0001F4F7 Photo'
        if last.media_type == 'video':
            return '\U0001F3A5 Video'
        if last.media_type == 'audio':
            return '\U0001F3A4 Voice message'

        legacy = self.context.get('legacy_media_map', {}).get(last.id)
        if legacy is None:
            first_item = last.media_items.first()
            legacy = first_item.media_type if first_item else None
        if legacy == 'image':
            return '\U0001F4F7 Photo'
        if legacy == 'video':
            return '\U0001F3A5 Video'
        if legacy == 'audio':
            return '\U0001F3A4 Voice message'
        return ''

    def get_timestamp(self, obj):
        last = self._get_last_message(obj)
        return last.created_at if last else None

    def get_avatar_user(self, obj):
        viewer = self.context.get('viewer')
        last = self._get_last_message(obj)
        if last and viewer and last.sender_id != viewer.id:
            return BasicUserSerializer(last.sender, context=self.context).data
        others = self._participants_excluding_viewer(obj)
        if others:
            return BasicUserSerializer(others[0], context=self.context).data
        return None

    def get_unread_count(self, obj):
        cached_map = self.context.get('unread_map')
        if cached_map is not None:
            return cached_map.get(obj.id, 0)
        viewer = self.context.get('viewer')
        if viewer is None:
            return 0
        return (
            obj.messages
            .filter(is_deleted=False)
            .exclude(sender_id=viewer.id)
            .exclude(read_by=viewer)
            .count()
        )
