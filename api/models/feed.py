# Auto-split from the former monolithic api/models.py by domain.
# All models keep app_label 'api' and identical fields, so this split is
# migration-neutral (verified via `makemigrations --check`). Re-exported
# from api/models/__init__.py so `from api.models import X` still works.

from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User


class NotInterested(models.Model):
    """
    An explicit "show me less of this" signal from a viewer.

    Three kinds, set by the long-press post menu:
      • post   — hide just this one post from discovery.
      • author — stop surfacing this author in discovery rails + a strong
                 negative affinity signal.
      • topic  — stop surfacing this hashtag in discovery rails + a strong
                 negative affinity signal.

    Read by services.feed_helpers.build_feed_context into exclusion sets that
    every discovery rail honours (alongside blocked / muted). The author and
    topic kinds also write a negative `not_interested` Activity row so the
    affinity profile down-weights them. See ACTIVITY_AND_FEED_AUDIT.md item B2.

    Exclusions are durable (they persist until the user undoes them) — unlike
    the 4-hour session "seen" set — because "stop showing me this person" is a
    standing preference, not a one-session thing.
    """
    KIND_POST = "post"
    KIND_AUTHOR = "author"
    KIND_TOPIC = "topic"
    KIND_CHOICES = (
        (KIND_POST, "Post"),
        (KIND_AUTHOR, "Author"),
        (KIND_TOPIC, "Topic"),
    )

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="not_interested"
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    # Exactly one of these is meaningful per row, keyed by `kind`.
    post = models.ForeignKey(
        'Post', on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    target_user = models.ForeignKey(
        User, on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    hashtag = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "kind"]),
        ]
        constraints = [
            # One row per (viewer, target) per kind — partial uniques so the
            # three kinds don't collide on each other's NULL columns. Makes
            # the endpoint's get_or_create idempotent under retries.
            models.UniqueConstraint(
                fields=["user", "post"],
                condition=models.Q(kind="post"),
                name="uniq_notinterested_post",
            ),
            models.UniqueConstraint(
                fields=["user", "target_user"],
                condition=models.Q(kind="author"),
                name="uniq_notinterested_author",
            ),
            models.UniqueConstraint(
                fields=["user", "hashtag"],
                condition=models.Q(kind="topic"),
                name="uniq_notinterested_topic",
            ),
        ]

    def __str__(self):
        tgt = self.hashtag or self.target_user_id or self.post_id
        return f"{self.user_id} not interested in {self.kind}:{tgt}"

class RecommendedAuthor(models.Model):
    """
    Precomputed collaborative-filtering output: "people with taste like
    yours engage with this author, and you don't yet."

    Written nightly by the `build_collaborative_recs` management command and
    read cheaply by api.feed.rails.collaborative._rail_collaborative — the heavy user-user
    similarity math runs offline, never on the request path. See
    ACTIVITY_AND_FEED_AUDIT.md item B3.

    `score` is the aggregated recommendation strength (similarity-weighted
    co-engagement); higher = stronger. One row per (viewer, author) pair.
    """
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="author_recommendations"
    )
    author = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="recommended_to"
    )
    score = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "author"], name="uniq_recommended_author"
            ),
        ]
        indexes = [
            # The rail's read: this viewer's recommendations, strongest first.
            models.Index(fields=["user", "-score"], name="recauthor_user_score_idx"),
        ]

    def __str__(self):
        return f"recommend {self.author_id} to {self.user_id} ({self.score:.2f})"

class UserAffinityProfile(models.Model):
    """
    Precomputed per-user taste profile for the home feed's activity rail.

    Written nightly by the `build_affinity_profiles` management command (which
    runs the expensive 30-day Activity scan offline) and read cheaply on the
    request path via api.feed.affinity._build_activity_profile — one indexed row
    lookup instead of scanning thousands of Activity rows per request. See
    ACTIVITY_AND_FEED_AUDIT.md item C4.

    `data` holds the serialized profile: author / hashtag / keyword affinity,
    per-time-of-day author & hashtag affinity, n_events, and discovery
    appetite. JSON stringifies the integer author / bucket keys; the reader
    (api.feed.affinity._normalize_profile) restores them.
    """
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="affinity_profile"
    )
    data = models.JSONField(default=dict)
    built_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"affinity profile for {self.user_id} @ {self.built_at:%Y-%m-%d %H:%M}"

class UserCloseFriends(models.Model):
    """
    Precomputed "very close friends" set for a user (UB-1).

    Mirrors UserAffinityProfile / RecommendedAuthor: the expensive scan runs
    OFFLINE, never on the request path. get_very_close_friend_ids() derives a
    ranked, weighted relationship set from the last 30 days of DMs, tags,
    comments, and likes between the viewer and others — for a creator whose
    posts attract thousands of likes/comments a day that scan can pull tens of
    thousands of rows into memory, and it used to run on every build_feed_context
    cache miss (per worker, every 90s). The nightly `build_close_friends`
    command now writes each active user's set here, and the request path reads
    one indexed row (services.feed_helpers.get_close_friend_ids).

    `friend_ids` is the JSON list of close-friend user ids, already capped by
    get_very_close_friend_ids' top-N limit. Read by the friend-network rail (B8)
    to heavily weight authors followed by the viewer's close friends.
    """
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="close_friends"
    )
    friend_ids = models.JSONField(default=list)
    built_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"close friends for {self.user_id} ({len(self.friend_ids or [])})"

class ProfileVisit(models.Model):
    CHANNEL_CHOICES = (("search", "Search"), ("direct", "Direct"), ("link", "Link"))
    SURFACE_CHOICES = (
        ("home", "Home Feed"), ("reels", "Reels"), ("profile", "Profile"),
        ("comments", "Comments"), ("messages", "Messages"),
        ("notifications", "Notifications"), ("explore", "Explore"),
        ("search", "Search Suggestions"), ("search_results", "Search Results"),
        ("history", "Search History"),
    )
    visitor = models.ForeignKey(User, on_delete=models.CASCADE, related_name="visits_made")
    visited_user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name="profile_visits")
    visited_page = models.ForeignKey("Page", on_delete=models.CASCADE, null=True, blank=True, related_name="page_visits")
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES)
    surface = models.CharField(max_length=20, choices=SURFACE_CHOICES)
    duration_seconds = models.PositiveIntegerField(
        default=0,
        help_text="Time spent on profile/page in seconds"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["visitor"]),
            models.Index(fields=["visited_user"]),
            models.Index(fields=["visited_page"]),
            models.Index(fields=["created_at"]),
        ]

class SearchHistory(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="search_history")
    query = models.CharField(max_length=255, blank=True, default="")
    searched_user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="appeared_in_search_history")
    searched_page = models.ForeignKey("Page", null=True, blank=True, on_delete=models.SET_NULL, related_name="appeared_in_search_history")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

class Activity(models.Model):
    """
    Unified per-user activity log. One row per discrete event.

    Coexists with the existing specialised tables (ProfileVisit, VideoWatch,
    ReelWatch, SearchHistory, PostLike, …) — those still drive feed ranking
    and analytics views. Activity gives us a single chronological stream we
    can query for ML / personalisation / audit.
    """

    ACTION_TYPES = (
        # — Posts —
        ("post_view",      "Post View"),         # full-screen / detail view, with duration
        # `post_impression` and `post_dwell` are deliberately separate events.
        #
        #   • post_impression — the post was placed into a feed slot and
        #     rendered to the user. Written server-side, in bulk, from
        #     api.feed.compose.compose_home_feed_page. No duration. Useful for
        #     CTR / impression-discounted ranking; NOT an engagement signal
        #     on its own.
        #
        #   • post_dwell — the user actually paused on the post while
        #     scrolling. Written client-side from FeedPost's dwell-timer,
        #     always carries a duration_seconds. A real "I stopped to look
        #     at this" signal.
        #
        # Before the split, impressions were being saved with the
        # "post_dwell" label and no duration, so the two events were
        # indistinguishable in SQL — see ACTIVITY_AND_FEED_AUDIT.md item A2.
        # The data migration 0071_relabel_impressions backfills historical
        # rows.
        ("post_impression","Post Impression"),   # rendered in a feed slot (server-logged)
        ("post_dwell",     "Post Dwell"),        # in-feed dwell while scrolling (client-logged)
        ("post_like",      "Post Like"),
        ("post_unlike",    "Post Unlike"),
        ("post_save",      "Post Save"),
        ("post_unsave",    "Post Unsave"),
        ("post_share",     "Post Share"),
        ("post_comment",   "Post Comment"),
        # How far the viewer scrolled through a post's comments. A "this post
        # is genuinely interesting" signal that's independent of whether they
        # liked it — written client-side from the Comments sheet, carrying a
        # depth fraction (0..1) in metadata. See ACTIVITY_AND_FEED_AUDIT.md
        # item D1.
        ("comment_scroll", "Comment Scroll Depth"),

        # — People & pages —
        ("user_visit",     "User Profile Visit"),
        ("page_visit",     "Page Visit"),

        # — Reels —
        ("reel_watch",     "Reel Watch"),        # base event with duration
        ("reel_complete",  "Reel Watched To End"),
        ("reel_rewatch",   "Reel Rewatch"),
        ("reel_skip",      "Reel Skip"),

        # — Search —
        ("search_query",   "Search Query"),
        ("search_click",   "Search Result Click"),

        # — Discovery / engagement —
        ("hashtag_engage", "Hashtag Engagement"),
        ("tab_view",       "Tab Viewed"),
        ("not_interested", "Not Interested"),   # explicit "show less" — negative signal
    )

    SURFACE_CHOICES = (
        ("home",           "Home Feed"),
        ("reels",          "Reels"),
        ("explore",        "Explore"),
        ("profile",        "Profile"),
        ("page",           "Page"),
        ("comments",       "Comments"),
        ("messages",       "Messages"),
        ("notifications",  "Notifications"),
        ("search",         "Search Suggestions"),
        ("search_results", "Search Results"),
        ("history",        "Search History"),
        ("post_detail",    "Post Detail"),
    )

    CHANNEL_CHOICES = (
        ("search", "Search"),
        ("direct", "Direct"),
        ("link",   "Link"),
        ("feed",   "Feed"),
    )

    SENTIMENT_LABELS = (
        ("positive",   "Positive"),
        ("neutral",    "Neutral"),
        ("negative",   "Negative"),
        ("question",   "Question"),
        ("intent_buy", "Purchase Intent"),
        ("mixed",      "Mixed"),
    )

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="activities"
    )
    action_type = models.CharField(max_length=30, choices=ACTION_TYPES)

    # Optional FKs — which one is set depends on action_type
    post = models.ForeignKey(
        "Post", on_delete=models.CASCADE, null=True, blank=True,
        related_name="activities",
    )
    page = models.ForeignKey(
        "Page", on_delete=models.CASCADE, null=True, blank=True,
        related_name="activities",
    )
    target_user = models.ForeignKey(
        User, on_delete=models.CASCADE, null=True, blank=True,
        related_name="activities_targeting_me",
    )
    comment = models.ForeignKey(
        "Comment", on_delete=models.CASCADE, null=True, blank=True,
        related_name="activities",
    )

    # Generic dimensions
    duration_seconds = models.FloatField(null=True, blank=True)
    surface = models.CharField(
        max_length=20, choices=SURFACE_CHOICES, blank=True, default=""
    )
    channel = models.CharField(
        max_length=20, choices=CHANNEL_CHOICES, blank=True, default=""
    )
    tab = models.CharField(max_length=30, blank=True, default="")

    # Reel-specific flags
    watched_to_end = models.BooleanField(default=False)
    is_rewatch     = models.BooleanField(default=False)
    is_skip        = models.BooleanField(default=False)

    # Search-specific
    query = models.CharField(max_length=255, blank=True, default="")

    # Comment sentiment / keyword analysis
    sentiment_label = models.CharField(
        max_length=20, choices=SENTIMENT_LABELS, blank=True, default=""
    )
    sentiment_score = models.FloatField(null=True, blank=True)  # -1.0 … 1.0
    keywords = models.JSONField(default=list, blank=True)       # ["niche:fashion","intent:purchase"]

    # Hashtag-specific
    hashtag = models.CharField(max_length=100, blank=True, default="")

    # Client-supplied session identifier (C3). A per-app-launch UUID sent in
    # the X-Session-Id header and captured automatically by SessionIdMiddleware
    # → log_activity, so every row from one visit shares an id. Lets us answer
    # "CTR within a single visit", "time to first action", and makes feed-
    # pipeline A/B testing clean. Blank when the client didn't send one.
    # NOTE: the session_id index lives in Meta.indexes below; we deliberately do
    # NOT also set db_index=True here -- that produced a SECOND, redundant index
    # on this write-heavy append-only table (IX-4).
    session_id = models.CharField(max_length=64, blank=True, default="")

    # Free-form payload for anything not modelled above
    metadata = models.JSONField(default=dict, blank=True)

    # db_index here is the ONLY standalone created_at index: the Meta indexes
    # below are all composites (user/-created_at, action_type/-created_at, ...)
    # which cannot serve a created_at-alone filter, and the nightly affinity /
    # close-friends jobs do filter(created_at__gte=...). Intentional, not a
    # duplicate -- do not remove (IX-4).
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["user", "action_type", "-created_at"]),
            models.Index(fields=["action_type", "-created_at"]),
            models.Index(fields=["post"]),
            models.Index(fields=["page"]),
            models.Index(fields=["target_user"]),
            models.Index(fields=["hashtag"]),
            models.Index(fields=["sentiment_label"]),
            models.Index(fields=["session_id"]),
        ]

    def __str__(self):
        return f"{self.user.username} · {self.action_type} @ {self.created_at:%Y-%m-%d %H:%M}"
