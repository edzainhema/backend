# Auto-split from the former monolithic api/serializers.py by domain.
# Re-exported from api/serializers/__init__.py so `from api.serializers import X`
# still works.

from rest_framework import serializers
from ..models import (
    Message,
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
    shared_post   = serializers.SerializerMethodField()
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
            'shared_post',
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

    def get_shared_post(self, obj):
        """
        Compact preview of a post the sender shared into this DM.
        Returns None when the message isn't a shared post or the post has
        since been deleted (FK is SET_NULL).
        """
        if obj.is_deleted or not obj.shared_post_id:
            return None
        post = obj.shared_post
        if post is None:
            return None
        request = self.context.get('request')

        thumb = None
        first_media_type = None
        for m in post.media.all().order_by('order'):
            if m.thumbnail:
                thumb = (
                    request.build_absolute_uri(m.thumbnail.url)
                    if request else m.thumbnail.url
                )
            elif m.file:
                thumb = (
                    request.build_absolute_uri(m.file.url)
                    if request else m.file.url
                )
            name = (m.file.name or '').lower() if m.file else ''
            if name.endswith(('.mp4', '.mov', '.webm', '.m4v')):
                first_media_type = 'video'
            else:
                first_media_type = 'image'
            break

        author = post.user
        author_avatar = None
        ap = getattr(author, 'userprofile', None)
        if ap and ap.avatar:
            author_avatar = (
                request.build_absolute_uri(ap.avatar.url)
                if request else ap.avatar.url
            )

        desc = (post.description or '')
        if len(desc) > 140:
            desc = desc[:140].rstrip() + '...'

        return {
            'id': post.id,
            'description': desc,
            'thumbnail': thumb,
            'media_type': first_media_type,
            'author': {
                'id': author.id,
                'username': author.username,
                'avatar': author_avatar,
            },
        }

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
      - legacy_media_map: { message_id: media_type }
      - unread_map:       { conversation_id: int }
    """
    conversation_id = serializers.IntegerField(source='id')
    name            = serializers.CharField()
    participants    = serializers.SerializerMethodField()
    last_message    = serializers.SerializerMethodField()
    timestamp       = serializers.SerializerMethodField()
    avatar_user     = serializers.SerializerMethodField()
    unread_count    = serializers.SerializerMethodField()

    def _participants_excluding_viewer(self, obj):
        viewer = self.context.get('viewer')
        if viewer is None:
            return list(obj.participants.all())
        return [p for p in obj.participants.all() if p.id != viewer.id]

    def _get_last_message(self, obj):
        cached_map = self.context.get('last_msg_map')
        if cached_map is not None:
            return cached_map.get(obj.id)
        return (
            obj.messages
            .select_related('sender__userprofile', 'shared_post', 'shared_post__user')
            .order_by('-created_at')
            .first()
        )

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
        # Shared-post preview: "You sent a post by <author>" /
        # "<sender> sent a post by <author>". Checked BEFORE the text branch
        # so a share with an optional caption still reads as a share in the
        # inbox. The shared_post FK is select_related'd in list_conversations
        # so this never issues a follow-up query.
        if last.shared_post_id:
            viewer = self.context.get('viewer')
            author_name = (
                last.shared_post.user.username
                if last.shared_post and last.shared_post.user
                else 'someone'
            )
            sender_is_viewer = viewer is not None and last.sender_id == viewer.id
            who = 'You' if sender_is_viewer else last.sender.username
            return f"{who} sent a post by {author_name}"
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
