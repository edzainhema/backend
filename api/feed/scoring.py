"""Shared scoring + candidate-annotation helpers used by every rail."""
from __future__ import annotations

import math

from django.db.models import Exists, OuterRef
from django.utils import timezone

from ..models import Follow, PageFollow, PostHashtag, PostLike, SavedPost
from .constants import CTR_RATE_SCALE, CTR_SMOOTHING, CTR_WEIGHT
from ..services.feed_helpers import (
    likes_count_subquery, comments_count_subquery, saves_count_subquery,
)

# =============================================================================
# HELPERS — math, geo, cursor, session dedup
# =============================================================================

def recency_decay_days(created_at, half_life_days: float) -> float:
    """
    Exponential decay: returns 1.0 for a brand-new post and ~0.5 at the
    half-life. Implemented in days because half-lives in this file are
    expressed in days for readability.
    """
    if created_at is None:
        return 0.0
    age_seconds = (timezone.now() - created_at).total_seconds()
    age_days = age_seconds / 86400.0
    return math.exp(-age_days / max(half_life_days, 0.0001))



# =============================================================================
# Candidate annotation — shared by every rail that needs viewer-context flags
# (likes/saves/follows) on Post objects before passing to serialize_post.
# =============================================================================

def _annotate_for_serialize(qs, user):
    """
    Attach the viewer-context fields that serialize_post checks for
    (`viewer_liked`, `viewer_saved`, `viewer_follows_author`,
    `viewer_follows_page`, plus the *_count_ann fields). Identical across
    rails so we don't drift between them.
    """
    return (
        qs
        .select_related("user", "user__userprofile", "page")
        .prefetch_related("media", "media__tags", "media__tags__user")
        .annotate(
            likes_count_ann=likes_count_subquery(),
            comments_count_ann=comments_count_subquery(),
            saves_count_ann=saves_count_subquery(),
            viewer_liked=Exists(
                PostLike.objects.filter(post=OuterRef("pk"), user=user)
            ),
            viewer_saved=Exists(
                SavedPost.objects.filter(post=OuterRef("pk"), user=user)
            ),
            viewer_follows_author=Exists(
                Follow.objects.filter(follower=user, following=OuterRef("user"))
            ),
            viewer_follows_page=Exists(
                PageFollow.objects.filter(user=user, page=OuterRef("page"))
            ),
        )
    )



def _engagement_log(likes: int, comments: int, saves: int) -> float:
    """Compressed RAW-magnitude engagement signal (popularity)."""
    return math.log10(1 + likes + 2 * comments + 3 * saves)



def _engagement_score(likes: int, comments: int, saves: int, impressions: int = 0) -> float:
    """
    Engagement signal that blends popularity with engagement RATE (C5).

    The old rails ranked on raw counts alone, so a post shown to 50k people
    with 100 likes scored the same as one shown to 100 people with 100 likes —
    even though the second converts vastly better per view. This adds a
    rate term: engagement-per-impression, smoothed by CTR_SMOOTHING pseudo-
    impressions so a 1-impression fluke can't spike (a Bayesian prior — with
    few impressions the rate is pulled toward 0; it approaches the true ratio
    only once enough impressions accumulate).

    Returns magnitude + a bonus, so it never drops below the old raw signal
    (popular posts keep their floor); high-CTR posts rise above it, and
    over-exposed low-converters get ~no bonus. `impressions` comes from the
    denormalized Post.impression_count (free to read; no per-request scan).
    """
    eng_points = likes + 2 * comments + 3 * saves
    magnitude = math.log10(1 + eng_points)
    rate = eng_points / (impressions + CTR_SMOOTHING)
    rate_bonus = CTR_WEIGHT * math.log10(1 + CTR_RATE_SCALE * rate)
    return magnitude + rate_bonus



def _exclude_not_interested(qs, context):
    """
    Apply the viewer's explicit "show me less" exclusions (B2) to a
    discovery-rail candidate queryset:

      • not-interested authors  → exclude by user_id
      • not-interested posts     → exclude by id
      • not-interested topics    → exclude any post carrying one of those
        hashtags, via the PostHashtag index (A8)

    Honoured by every discovery rail (friend-network, nearby, activity,
    trending) but NOT the followed rail — "not interested in this post"
    shouldn't blank out someone you deliberately follow (that's what
    unfollow / mute are for).
    """
    ni_users = context.get("not_interested_user_ids")
    if ni_users:
        qs = qs.exclude(user_id__in=ni_users)
    ni_posts = context.get("not_interested_post_ids")
    if ni_posts:
        qs = qs.exclude(id__in=ni_posts)
    ni_tags = context.get("not_interested_hashtags")
    if ni_tags:
        qs = qs.exclude(
            id__in=PostHashtag.objects.filter(hashtag__in=ni_tags).values("post_id")
        )
    return qs



# =============================================================================
# Cross-rail deduplication
# =============================================================================

def _percentile_ranks(values: list[float]) -> dict[int, float]:
    """
    Map each value to its percentile rank in [0,1] within `values` — the
    fraction of the list it ranks at or above, with tied values sharing the
    average rank.

    This is the cross-rail comparison primitive (C2). It replaces a z-score,
    which assumed each rail's scores were roughly normal and on a comparable
    spread — they aren't. Feed scores are skewed (a handful of viral posts
    sit far above the pack), and z-score's standard-deviation denominator gets
    inflated by those outliers, distorting where everything else lands.
    Percentile rank is distribution-free and outlier-robust: it only cares
    about ORDER, so "top of its rail" maps to ~1.0 regardless of the rail's
    raw scale or shape, which is exactly what we want when deciding which rail
    a shared post belongs to.

    (For an even more stable comparison one could rank each post against its
    rail's full score distribution sampled periodically, rather than the
    per-page candidate slice; that needs score-sampling infrastructure and is
    a further refinement. Ranking within the slice already removes the
    normality/scale assumptions the audit flagged as "rough math".)
    """
    n = len(values)
    if n == 0:
        return {}
    if n == 1:
        return {0: 1.0}
    order = sorted(range(n), key=lambda i: values[i])
    ranks: dict[int, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        # Indices order[i..j] are tied; give them the average position,
        # normalized to [0,1] (lowest → 0.0, highest → 1.0).
        pct = ((i + j) / 2.0) / (n - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = pct
        i = j + 1
    return ranks
