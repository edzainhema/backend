# Auto-split from the former monolithic api/models.py by domain.
# All models keep app_label 'api' and identical fields, so this split is
# migration-neutral (verified via `makemigrations --check`). Re-exported
# from api/models/__init__.py so `from api.models import X` still works.

from django.db import models
from django.contrib.auth.models import User


class Media(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    file = models.FileField(upload_to='uploads/', default='defaults/default.jpg')
    uploaded_at = models.DateTimeField(auto_now_add=True)

class Comment(models.Model):
    post = models.ForeignKey("Post", on_delete=models.CASCADE, related_name="comments", db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="comments")
    parent = models.ForeignKey("self", on_delete=models.CASCADE, related_name="replies", null=True, blank=True)
    text = models.TextField(blank=True)
    file = models.FileField(upload_to="comment_media/", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            # get_comments filters by post and orders by created_at; the post FK
            # index narrows rows but the per-post ordering would otherwise be a
            # filesort on each page (painful for a viral post). (post, created_at)
            # serves both the filter and the ordering. See
            # BACKEND_SCALING_AUDIT.md IX-3.
            models.Index(
                fields=["post", "created_at"],
                name="comment_post_created_idx",
            ),
        ]

    def __str__(self):
        if self.is_deleted:
            return f"Deleted comment ({self.id})"
        return f"{self.user.username} on post {self.post_id}"

class CommentMention(models.Model):
    comment = models.ForeignKey('Comment', on_delete=models.CASCADE, related_name="mentions")
    mentioned_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mentioned_in_comments")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("comment", "mentioned_user")

class Post(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    page = models.ForeignKey('Page', on_delete=models.CASCADE, null=True, blank=True, related_name="posts")
    description = models.TextField(blank=True)
    # Human-readable location label the poster optionally typed in the
    # upload sheet ("Brooklyn Bridge", "Mom's house", etc.). Distinct
    # from the GPS coordinates below — the user picks this string and
    # it's displayed verbatim on the post.
    location = models.CharField(max_length=255, blank=True, default="")
    # GPS coordinates of the device at the moment the user hit Share,
    # captured by ProcessUpload via the cached/fresh fix from
    # frontend/src/utils/permissions.ts. Nullable because not every
    # user grants location permission, and that's fine — null just
    # means the feed-ranker has no geo signal for this post and falls
    # back to non-geo ranking. We deliberately store the raw lat/lng
    # rather than a reverse-geocoded place name: the latter goes stale
    # (place names change), and we already have the `location` string
    # above for human-readable labels.
    upload_latitude = models.FloatField(null=True, blank=True)
    upload_longitude = models.FloatField(null=True, blank=True)
    # Horizontal accuracy reported by the OS, in meters. Useful for
    # downweighting fixes from coarse cell-tower triangulation vs.
    # precise GPS when ranking nearby posts. Null when the OS didn't
    # include it (some Android sources omit accuracy entirely).
    upload_accuracy_m = models.FloatField(null=True, blank=True)
    is_public_override = models.BooleanField(default=False)
    # Denormalized count of how many times this post has been rendered into a
    # feed slot (one bump per home-feed render). The feed ranker reads this
    # off the Post row to compute an engagement RATE (engagement ÷ impressions)
    # instead of ranking on raw engagement counts — so a post shown to 50k
    # people with 100 likes doesn't outrank one shown to 100 people with 100
    # likes. See ACTIVITY_AND_FEED_AUDIT.md item C5. The per-impression
    # Activity rows (action_type="post_impression") remain the source of
    # truth; this is just the fast-read aggregate, kept current by an F()+1
    # update on render and backfilled from those rows in migration 0078.
    impression_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            # Feed query: filter by user, order by -created_at
            models.Index(fields=['user', '-created_at', 'id'], name='post_user_created_idx'),
            # Feed query: filter by page, order by -created_at
            models.Index(fields=['page', '-created_at', 'id'], name='post_page_created_idx'),
            # Cursor-based pagination: compound (created_at, id) filter
            models.Index(fields=['-created_at', 'id'], name='post_created_id_idx'),
        ]

    def __str__(self):
        return f"Post {self.id} by {self.user.username}"

class PostMedia(models.Model):
    post = models.ForeignKey('Post', on_delete=models.CASCADE, related_name="media", db_index=True)
    file = models.FileField(upload_to="uploads/")
    thumbnail = models.ImageField(upload_to="thumbnails/", null=True, blank=True)
    order = models.PositiveIntegerField(default=0)
    # Pixel dimensions of the underlying media. Captured at upload time so
    # the feed can size each tile before the asset finishes loading,
    # eliminating the per-image Image.getSize() request the client used to
    # make for layout. Nullable: legacy rows from before this column
    # existed, and any future media kind we can't measure, leave these as
    # NULL and the frontend falls back to the runtime sizing path.
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)

    def __str__(self):
        return f"Media {self.id} for Post {self.post_id}"

class PostMediaTag(models.Model):
    media = models.ForeignKey("PostMedia", on_delete=models.CASCADE, related_name="tags")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    x = models.FloatField(null=True, blank=True)
    y = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("media", "user")

class PostHashtag(models.Model):
    """
    Denormalized one-row-per-(post, hashtag) index of the hashtags that
    appear in a post's description.

    Exists so the feed ranker can answer "which posts carry any of this
    viewer's favourite hashtags?" precisely and cheaply. The previous
    approach scanned Post.description with `description__icontains="#blue"`,
    which (a) matched substrings — "#blue" hit "#blueberry", "#bluetooth" —
    and (b) was an unindexed full-text scan that got slower as the post
    table grew. See ACTIVITY_AND_FEED_AUDIT.md item A8.

    Populated by api.utils.sync_post_hashtags on post create (and on edit,
    if an edit path is ever added — the sync is diff-based and idempotent).
    Tags are stored lowercased and WITHOUT the leading '#', matching
    comment_analyzer.extract_hashtags' output and the Activity.hashtag /
    affinity-profile vocabulary, so an exact `hashtag__in=[...]` join lines
    up across the whole system.
    """
    post = models.ForeignKey(
        'Post', on_delete=models.CASCADE, related_name="hashtags"
    )
    # max_length mirrors Activity.hashtag (100). extract_hashtags itself
    # only captures up to 50 chars today, but keeping the column wider than
    # the extractor means a future regex change can't start raising on save.
    hashtag = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # One row per tag per post. extract_hashtags already de-dupes,
            # but the constraint makes bulk_create(ignore_conflicts=True)
            # safe under retries / races and guarantees the invariant at
            # the DB level.
            models.UniqueConstraint(
                fields=["post", "hashtag"], name="uniq_post_hashtag"
            ),
        ]
        indexes = [
            # Covering index for the hot feed query:
            #   SELECT post_id FROM api_posthashtag WHERE hashtag IN (...)
            # Both columns live in the index, so the lookup never touches
            # the table heap. Ordering (hashtag, post) also makes the
            # IN-list probe a series of index range scans.
            models.Index(fields=["hashtag", "post"], name="posthashtag_tag_post_idx"),
        ]

    def __str__(self):
        return f"#{self.hashtag} on Post {self.post_id}"

class PostLike(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    post = models.ForeignKey('Post', on_delete=models.CASCADE, related_name="likes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "post")

    def __str__(self):
        return f"{self.user.username} liked Post {self.post_id}"

class SavedPost(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    post = models.ForeignKey('Post', on_delete=models.CASCADE, related_name="saved_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "post")

    def __str__(self):
        return f"{self.user.username} saved Post {self.post_id}"

class CommentLike(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    comment = models.ForeignKey('Comment', on_delete=models.CASCADE, related_name="likes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "comment")

    def __str__(self):
        return f"{self.user.username} liked comment {self.comment_id}"

class ReelWatch(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    post = models.ForeignKey("Post", on_delete=models.CASCADE)
    seconds_watched = models.FloatField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "post")

class VideoWatch(models.Model):
    CHANNEL_CHOICES = (("search", "Search"), ("direct", "Direct"), ("link", "Link"))
    SURFACE_CHOICES = (
        ("reels", "Reels"), ("explore", "Explore"), ("profile", "Profile"),
        ("page", "Page"), ("home", "Home Feed"),
        ("search", "Search Suggestions"), ("search_results", "Search Results"),
    )
    viewer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="video_watches")
    post = models.ForeignKey("Post", on_delete=models.CASCADE, related_name="video_watches")
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES)
    surface = models.CharField(max_length=20, choices=SURFACE_CHOICES)
    duration_seconds = models.PositiveIntegerField(
        default=0,
        help_text="Time watched in seconds"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["viewer"]),
            models.Index(fields=["post"]),
            models.Index(fields=["created_at"]),
        ]
