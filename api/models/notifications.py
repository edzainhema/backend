# Auto-split from the former monolithic api/models.py by domain.
# All models keep app_label 'api' and identical fields, so this split is
# migration-neutral (verified via `makemigrations --check`). Re-exported
# from api/models/__init__.py so `from api.models import X` still works.

from django.db import models
from django.contrib.auth.models import User


class Notification(models.Model):
    NOTIFICATION_TYPES = (
        ("follow",                "Follow"),
        ("follow_request",        "Follow Request"),
        ("follow_approved",       "Follow Approved"),
        ("like",                  "Like"),
        ("comment",               "Comment"),
        ("comment_reply",         "Comment Reply"),
        ("comment_like",          "Comment Like"),
        ("mention",               "Mention"),
        ("page_invite",           "Page Invite"),
        ("page_follow",           "Page Follow"),
        ("page_follow_request",   "Page Follow Request"),
        ("page_follow_approved",  "Page Follow Approved"),
        ("page_poster_added",     "Page Poster Added"),
    )

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    actor = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sent_notifications")
    notification_type = models.CharField(max_length=30, choices=NOTIFICATION_TYPES)
    media = models.ForeignKey("Post", on_delete=models.CASCADE, null=True, blank=True)
    comment = models.ForeignKey("Comment", on_delete=models.CASCADE, null=True, blank=True)
    page = models.ForeignKey("Page", on_delete=models.CASCADE, null=True, blank=True, related_name="notifications")
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # list_notifications: filter(recipient=user).order_by("-created_at").
            # Serves both the filter and the ordered scan, so no per-page filesort.
            models.Index(
                fields=["recipient", "-created_at"],
                name="notif_recipient_created_idx",
            ),
            # unread bell badge: filter(recipient=user, is_read=False).count(),
            # polled on every Home focus -- this stops it scanning all of the
            # recipient's notifications. See BACKEND_SCALING_AUDIT.md IX-1.
            models.Index(
                fields=["recipient", "is_read"],
                name="notif_recipient_unread_idx",
            ),
        ]

    def __str__(self):
        return f"{self.actor.username} → {self.recipient.username} ({self.notification_type})"
