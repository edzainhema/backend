# Auto-split from the former monolithic api/models.py by domain.
# All models keep app_label 'api' and identical fields, so this split is
# migration-neutral (verified via `makemigrations --check`). Re-exported
# from api/models/__init__.py so `from api.models import X` still works.

from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User


class PostReport(models.Model):
    REPORT_REASONS = (
        ("spam", "Spam"),
        ("nudity", "Nudity or sexual content"),
        ("violence", "Violence or dangerous acts"),
        ("hate", "Hate or harassment"),
        ("false_info", "False information"),
        ("other", "Other"),
    )
    reporter = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reported_posts")
    post = models.ForeignKey('Post', on_delete=models.CASCADE, related_name="reports")
    reason = models.CharField(max_length=30, choices=REPORT_REASONS)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("reporter", "post")
        ordering = ["-created_at"]

class MutedUser(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="muted_users")
    muted_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="muted_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "muted_user")
        indexes = [
            # Muted-users list keyset pagination: filter by `user`, order by
            # (-created_at, -id).
            models.Index(
                fields=['user', '-created_at', '-id'],
                name='muteduser_user_created_idx',
            ),
        ]

class BlockedUserQuerySet(models.QuerySet):
    """
    Query helpers for the symmetric block relationship.

    Blocks are stored one-directionally (user blocked blocked_user), but almost
    every gate in the app cares about *either* direction. These two methods are
    the single source of truth for that logic — previously the same
    `Q(user=a, blocked_user=b) | Q(user=b, blocked_user=a)` was hand-written at
    40+ call sites, so a fix or typo in one couldn't be trusted to hold in the
    others. Accept either User instances or ids; Django resolves both for the FK.
    """

    def between(self, a, b):
        """
        Block rows in EITHER direction between two users. Use `.exists()` for
        the common "is there a block between these two?" 403 gate:

            if BlockedUser.objects.between(request.user, other).exists():
                return Response({"error": "Not allowed"}, status=403)
        """
        return self.filter(
            Q(user=a, blocked_user=b) | Q(user=b, blocked_user=a)
        )

    def involving(self, user):
        """
        Every block row where `user` is the blocker OR the blocked party.
        Used to build the "hide both directions" exclusion set:

            pairs = BlockedUser.objects.involving(user).values_list(
                "user_id", "blocked_user_id"
            )
        """
        return self.filter(Q(user=user) | Q(blocked_user=user))

class BlockedUser(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="blocked_users")
    blocked_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="blocked_by")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = BlockedUserQuerySet.as_manager()

    class Meta:
        unique_together = ("user", "blocked_user")
        indexes = [
            # Blocked-users list keyset pagination: filter by `user`, order by
            # (-created_at, -id).
            models.Index(
                fields=['user', '-created_at', '-id'],
                name='blockeduser_user_created_idx',
            ),
        ]

class UserReport(models.Model):
    REPORT_REASONS = (
        ("spam", "Spam"),
        ("harassment", "Harassment or hate"),
        ("impersonation", "Impersonation"),
        ("nudity", "Nudity or sexual content"),
        ("violence", "Violence or dangerous acts"),
        ("other", "Other"),
    )
    reporter = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reported_users")
    reported_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reports_against")
    reason = models.CharField(max_length=30, choices=REPORT_REASONS)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("reporter", "reported_user")
        ordering = ["-created_at"]
