# Auto-split from the former monolithic api/models.py by domain.
# All models keep app_label 'api' and identical fields, so this split is
# migration-neutral (verified via `makemigrations --check`). Re-exported
# from api/models/__init__.py so `from api.models import X` still works.

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    bio = models.TextField(blank=True)

    is_private = models.BooleanField(default=False)
    memories_public = models.BooleanField(default=True)
    last_username_change = models.DateTimeField(null=True, blank=True)

    # Whether this account's `User.email` has been proven to belong to the
    # account holder. Local registration cannot prove it (there is no
    # verification-email flow), so password-created accounts start False; a
    # social login whose provider reports the email as verified flips it True.
    # This is the trust flag the social-login path consults before it will
    # adopt a pre-existing account, which is what closes the account
    # pre-hijacking vector. See services.auth_helpers._login_or_create_social_user.
    email_verified = models.BooleanField(default=False)

    avatar = models.ImageField(
        upload_to="avatars/",
        blank=True,
        null=True,
        default="avatars/default.png"
    )

    # ✅ NEW: presence tracking — updated on every authenticated request
    last_seen = models.DateTimeField(null=True, blank=True, db_index=True)

    # ─── Personalization: latest device-reported location ──────────────
    # Sent up by the frontend after the user grants the location
    # permission on first launch (and refreshed on every cold start /
    # foreground while permission is still granted). Used by the home
    # feed / pages search to rank near-by content first; never exposed
    # to other users. Stored as nullable so a user who declined the
    # permission simply has (None, None) and falls back to non-geo
    # ranking. db_index on latitude alone isn't useful for a 2-D query,
    # but we index `location_updated_at` so we can cheaply skip
    # personalization for users whose fix is stale.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    # Reported accuracy radius (meters) from the device. Lets the feed
    # ranker weight very-coarse fixes (cell-tower / VPN) less than
    # GPS-level ones, and lets us decide whether to bother refreshing.
    location_accuracy_m = models.FloatField(null=True, blank=True)
    location_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)

    def can_change_username(self):
        if not self.last_username_change:
            return True
        return (timezone.now() - self.last_username_change).days >= 365

    @property
    def is_online(self):
        """True if last_seen within the last 3 minutes."""
        if not self.last_seen:
            return False
        return (timezone.now() - self.last_seen).total_seconds() < 180

    def __str__(self):
        return f"{self.user.username}"

class Follow(models.Model):
    follower = models.ForeignKey(User, on_delete=models.CASCADE, related_name='following')
    # db_index=True ensures single-column lookups on `following` (e.g. viewer_followers
    # query) use an index. unique_together only covers (follower, following) together.
    following = models.ForeignKey(User, on_delete=models.CASCADE, related_name='followers', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('follower', 'following')
        indexes = [
            # Followers list keyset pagination: filter by `following`, order by
            # (-created_at, -id). Composite index lets the DB satisfy both the
            # filter and the ordered range scan without a separate sort.
            models.Index(
                fields=['following', '-created_at', '-id'],
                name='follow_following_created_idx',
            ),
        ]

    def __str__(self):
        return f"{self.follower} -> {self.following}"

class FollowRequest(models.Model):
    requester = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_follow_requests')
    target = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_follow_requests')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('requester', 'target')

    def __str__(self):
        return f"{self.requester} → {self.target} (pending)"
