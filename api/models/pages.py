# Auto-split from the former monolithic api/models.py by domain.
# All models keep app_label 'api' and identical fields, so this split is
# migration-neutral (verified via `makemigrations --check`). Re-exported
# from api/models/__init__.py so `from api.models import X` still works.

from django.db import models
from django.contrib.auth.models import User


class Page(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='owned_pages')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_private = models.BooleanField(default=False)
    is_super_private = models.BooleanField(default=False)
    anyone_can_post = models.BooleanField(default=True)
    avatar = models.ImageField(
        upload_to="page_avatars/",
        blank=True,
        null=True,
        default="page_avatars/default.png"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    is_event = models.BooleanField(default=False)
    event_date = models.DateField(null=True, blank=True)
    event_time = models.TimeField(null=True, blank=True)
    event_location = models.CharField(max_length=255, blank=True)
    # Structured location captured when the owner picks a Google Places
    # suggestion in LocationModal. event_location is the short display name
    # shown in the UI (e.g. "Toothy Moose Cabaret"); event_address holds the
    # full formatted address ("1661 Argyle St, Halifax, NS ...") revealed when
    # a viewer taps the location. event_latitude/longitude/place_id add the
    # geocoded point + Google id for maps/directions. For free-text entry only
    # event_location is set and the rest stay blank.
    event_address = models.CharField(max_length=255, blank=True, default="")
    event_latitude = models.FloatField(null=True, blank=True)
    event_longitude = models.FloatField(null=True, blank=True)
    event_place_id = models.CharField(max_length=255, blank=True, default="")

    # When True, the page exposes a group chat to all followers.
    chat_enabled = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} by {self.owner}"

class PageChatMessage(models.Model):
    """
    A single message in a page-wide group chat.

    All followers of a page (plus the page owner) can read and post when the
    page's `chat_enabled` flag is True.

    Media support mirrors the DM `Message` model: legacy single-attachment
    messages use the `media` + `media_type` fields directly; multi-attachment
    messages additionally fan their files out into `PageChatMessageMedia` rows
    so the client can render a grid. The legacy fields stay populated for the
    first attachment in either case so older clients keep working.
    """
    page = models.ForeignKey(
        'Page',
        on_delete=models.CASCADE,
        related_name="chat_messages",
    )
    sender = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="page_chat_messages",
    )
    # `blank=True` so media-only messages (e.g. a voice note) can save with
    # no caption.
    text = models.TextField(blank=True)

    media = models.FileField(upload_to="page_chat_media/", null=True, blank=True)
    media_type = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        choices=[("image", "Image"), ("video", "Video"), ("audio", "Audio")],
    )

    created_at = models.DateTimeField(auto_now_add=True)

    # Self-referential reply target, mirroring the DM `Message` model so a page
    # chat message can quote an earlier one. SET_NULL keeps a reply intact if
    # the quoted message is later removed.
    reply_to = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='replies',
    )

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["page", "created_at", "id"], name="pagechat_page_time_idx"),
        ]

    def __str__(self):
        return f"{self.sender} → {self.page}: {self.text[:30]}"

class PageChatMessageMedia(models.Model):
    """One attachment in a multi-attachment PageChatMessage. The first row's
    file is also mirrored into PageChatMessage.media so legacy single-media
    serialization keeps working."""
    message = models.ForeignKey(
        'PageChatMessage',
        on_delete=models.CASCADE,
        related_name="media_items",
    )
    file = models.FileField(upload_to="page_chat_media/")
    media_type = models.CharField(
        max_length=10,
        choices=[("image", "Image"), ("video", "Video"), ("audio", "Audio")],
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        indexes = [models.Index(fields=["message"])]

    def __str__(self):
        return f"PageChatMessageMedia {self.id} ({self.media_type}) for msg {self.message_id}"

class PageFollow(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='followed_pages')
    page = models.ForeignKey('Page', on_delete=models.CASCADE, related_name='followers')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'page')
        indexes = [
            # Page followers list keyset pagination: filter by `page`, order by
            # (-created_at, -id). Composite index serves both filter and the
            # ordered range scan without a separate sort.
            models.Index(
                fields=['page', '-created_at', '-id'],
                name='pagefollow_page_created_idx',
            ),
        ]

    def __str__(self):
        return f"{self.user} follows {self.page}"

class PageFollowRequest(models.Model):
    requester = models.ForeignKey(User, on_delete=models.CASCADE, related_name='page_follow_requests')
    page = models.ForeignKey('Page', on_delete=models.CASCADE, related_name='pending_requests')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('requester', 'page')

class PageInvite(models.Model):
    page = models.ForeignKey('Page', on_delete=models.CASCADE, related_name="invites")
    invited_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sent_page_invites")
    invited_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="received_page_invites")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("page", "invited_user")
        indexes = [
            # Sent-invites list keyset pagination: filter by `page`, order by
            # (-created_at, -id).
            models.Index(
                fields=['page', '-created_at', '-id'],
                name='pageinvite_page_created_idx',
            ),
        ]

    def __str__(self):
        return f"{self.invited_by.username} invited {self.invited_user.username} → {self.page.name}"

class Memory(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='memories')
    page = models.ForeignKey('Page', on_delete=models.CASCADE, related_name='saved_by')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'page')

    def __str__(self):
        return f"{self.user} saved {self.page}"

class PagePoster(models.Model):
    page = models.ForeignKey('Page', on_delete=models.CASCADE, related_name="allowed_posters")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("page", "user")
        indexes = [
            # Allowed-posters list keyset pagination: filter by `page`, order by
            # (-added_at, -id).
            models.Index(
                fields=['page', '-added_at', '-id'],
                name='pageposter_page_added_idx',
            ),
        ]

class PinnedPage(models.Model):
    """A page a user has pinned to their own profile.

    Mirrors the Instagram-highlights pattern: the viewer chooses pages to
    showcase, and they render as a horizontal row of circular avatars under
    the Follow / Message buttons on the user's profile. The relationship is
    (user, page) — "this user pinned this page" — so a profile's pinned row
    is just `PinnedPage.objects.filter(user=<profile owner>)`.

    Ordered most-recently-pinned first via the composite index below.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="pinned_pages")
    page = models.ForeignKey('Page', on_delete=models.CASCADE, related_name="pinned_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "page")
        indexes = [
            # Pinned-pages row: filter by `user`, order newest-first by
            # (-created_at, -id). One composite index serves both the filter
            # and the ordering without a separate sort.
            models.Index(
                fields=['user', '-created_at', '-id'],
                name='pinnedpage_user_created_idx',
            ),
        ]

    def __str__(self):
        return f"{self.user} pinned {self.page}"

class MutedPage(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="muted_pages")
    page = models.ForeignKey('Page', on_delete=models.CASCADE, related_name="muted_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "page")

class PageReport(models.Model):
    REPORT_REASONS = (
        ("spam", "Spam"),
        ("impersonation", "Impersonation"),
        ("nudity", "Nudity or sexual content"),
        ("violence", "Violence or dangerous acts"),
        ("hate", "Hate or harassment"),
        ("scam", "Scam or fraud"),
        ("other", "Other"),
    )
    reporter = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reported_pages")
    page = models.ForeignKey('Page', on_delete=models.CASCADE, related_name="reports")
    reason = models.CharField(max_length=30, choices=REPORT_REASONS)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("reporter", "page")
        ordering = ["-created_at"]
