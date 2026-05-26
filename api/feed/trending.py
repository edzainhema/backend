"""Trending-hashtag computation and cached lookup."""
from __future__ import annotations
import logging

from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count
from django.utils import timezone

from ..models import PostHashtag
from .constants import TRENDING_HASHTAG_KEY, TRENDING_HASHTAG_MAX, TRENDING_HASHTAG_MIN_POSTS, TRENDING_HASHTAG_TTL_S, TRENDING_HASHTAG_WINDOW_MINUTES

logger = logging.getLogger(__name__)

# =============================================================================
# Trending hashtags (D2)
# =============================================================================
#
# "Once the proper hashtag table exists (A8), a rolling count of new posts per
# hashtag gives you trending hashtags for free." This pair computes that count
# and the activity rail uses it to time-weight a viewer's hashtag interest.

def compute_trending_hashtags() -> dict[str, float]:
    """
    Count posts per hashtag created in the last TRENDING_HASHTAG_WINDOW_MINUTES
    (via the PostHashtag index joined to Post.created_at), keep the tags at or
    above TRENDING_HASHTAG_MIN_POSTS, and return {hashtag: intensity}, where
    intensity is the tag's count scaled to (0, 1] by the busiest tag in the
    window. Intensity lets the ranker give the hottest tags the full boost and
    merely-warm ones a fraction.

    Note we filter on Post.created_at (the post's age), NOT PostHashtag.created_at
    — the backfill migration (0073) stamped every historical hashtag row with
    the backfill time, so PostHashtag.created_at is meaningless for "new posts".

    Returns {} when nothing qualifies. Best-effort: never raises (the caller
    treats {} as "no boost").
    """
    try:
        since = timezone.now() - timedelta(minutes=TRENDING_HASHTAG_WINDOW_MINUTES)
        rows = (
            PostHashtag.objects
            .filter(post__created_at__gte=since)
            .values("hashtag")
            .annotate(n=Count("post_id", distinct=True))
            .filter(n__gte=TRENDING_HASHTAG_MIN_POSTS)
            .order_by("-n")[:TRENDING_HASHTAG_MAX]
        )
        counts = {r["hashtag"]: r["n"] for r in rows}
    except Exception as exc:
        logger.error(f"[compute_trending_hashtags] failed: {exc}")
        return {}

    if not counts:
        return {}
    max_n = max(counts.values())
    if max_n <= 0:
        return {}
    return {h: n / max_n for h, n in counts.items()}



def get_trending_hashtags() -> dict[str, float]:
    """
    Read the {hashtag: intensity} map the build_trending_hashtags command
    refreshes into cache every few minutes. Returns {} on a cold cache, a
    Redis outage, or when nothing is trending — and every caller treats an
    empty map as "apply no boost", so the feature is completely safe before
    the job has ever run.

    Self-heals with a single bounded compute when the cache is cold so a fresh
    deploy isn't un-boosted until the next cron tick; the result is written
    back so concurrent requests in the same window don't each recompute.
    """
    try:
        data = cache.get(TRENDING_HASHTAG_KEY)
    except Exception:
        data = None

    if data is None:
        data = compute_trending_hashtags()
        try:
            cache.set(TRENDING_HASHTAG_KEY, data, timeout=TRENDING_HASHTAG_TTL_S)
        except Exception:
            pass

    return data or {}
