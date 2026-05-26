# Auto-split from the former monolithic api/models.py by domain.
# All models keep app_label 'api' and identical fields, so this split is
# migration-neutral (verified via `makemigrations --check`). Re-exported
# from api/models/__init__.py so `from api.models import X` still works.

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class Conversation(models.Model):
    participants = models.ManyToManyField(User, related_name='conversations')

    # ✅ NEW: optional display name for group chats
    name = models.CharField(max_length=100, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    # ✅ NEW: bumped every time a new message arrives — drives ConversationList sort order
    updated_at = models.DateTimeField(default=timezone.now, db_index=True)

    def __str__(self):
        if self.name:
            return f"Group '{self.name}' ({self.id})"
        return f"Conversation {self.id}"

class Message(models.Model):
    conversation = models.ForeignKey('Conversation', on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_messages')
    text = models.TextField(blank=True)

    media = models.FileField(upload_to='message_media/', null=True, blank=True)
    media_type = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        choices=[('image', 'Image'), ('video', 'Video'), ('audio', 'Audio')],
    )

    created_at = models.DateTimeField(auto_now_add=True)

    read_by = models.ManyToManyField(User, related_name="read_messages", blank=True)

    reply_to = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='replies',
    )

    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    # ✅ NEW: edit support
    is_edited = models.BooleanField(default=False)
    last_edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            # DM thread load -- filter(conversation).order_by("-created_at") --
            # and list_conversations' per-conversation latest-message / unread
            # aggregates. Mirrors PageChatMessage.pagechat_page_time_idx; the id
            # tiebreaker keeps ordering stable for same-timestamp messages and
            # serves keyset slicing. See BACKEND_SCALING_AUDIT.md IX-2.
            models.Index(
                fields=["conversation", "created_at", "id"],
                name="message_convo_time_idx",
            ),
        ]

    def soft_delete(self):
        """
        Soft-delete this message and HARD-delete its media from storage.

        The row is kept (``is_deleted=True``) so the conversation timeline and
        any replies that quote this message stay intact, but the underlying
        photo / video must actually be removed:

          * Privacy — a deleted message is meant to be retracted, so leaving
            the file downloadable at its still-valid media URL defeats the
            point.
          * Storage — orphaned blobs no row references would otherwise
            accumulate forever.

        Mirrors the blob cleanup ``delete_comment`` does for comment files.
        Handles BOTH the legacy single-attachment ``media`` field and the
        multi-attachment ``MessageMedia`` children. Idempotent (a second call
        is a no-op) and best-effort on storage: a hiccup deleting one blob
        still lets the row be marked deleted — a background sweeper can reclaim
        a stray file from the now-cleared references.

        Callers own the permission / block checks and the WS broadcast; this
        only performs the deletion, so the REST endpoint and the WS consumer
        share one implementation and can't drift apart.
        """
        if self.is_deleted:
            return

        # Legacy single-attachment blob: .delete(save=False) removes the file
        # from storage and clears the field on the instance (persisted below).
        if self.media:
            try:
                self.media.delete(save=False)
            except Exception:
                pass

        # Multi-attachment children: delete each blob, then the rows.
        # QuerySet.delete() does NOT call FileField.delete(), so the files must
        # go first or they'd be orphaned.
        media_items = list(self.media_items.all())
        for item in media_items:
            if item.file:
                try:
                    item.file.delete(save=False)
                except Exception:
                    pass
        if media_items:
            self.media_items.all().delete()

        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.text = ""
        self.media_type = None
        self.save(update_fields=[
            "is_deleted", "deleted_at", "text", "media", "media_type",
        ])

    def __str__(self):
        return f"{self.sender.username}: {self.text[:30]}"

class MessageReaction(models.Model):
    message = models.ForeignKey('Message', on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="message_reactions")
    emoji = models.CharField(max_length=8)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("message", "user")
        indexes = [models.Index(fields=["message"])]

    def __str__(self):
        return f"{self.user.username} reacted {self.emoji} to msg {self.message_id}"

class MessageMedia(models.Model):
    message = models.ForeignKey('Message', on_delete=models.CASCADE, related_name="media_items")
    file = models.FileField(upload_to="message_media/")
    media_type = models.CharField(
        max_length=10,
        choices=[("image", "Image"), ("video", "Video"), ("audio", "Audio")],
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        indexes = [models.Index(fields=["message"])]

    def __str__(self):
        return f"MessageMedia {self.id} ({self.media_type}) for msg {self.message_id}"

class ConversationHidden(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="hidden_conversations")
    conversation = models.ForeignKey('Conversation', on_delete=models.CASCADE, related_name="hidden_by")
    hidden_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "conversation")
