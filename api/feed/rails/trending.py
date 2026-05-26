"""Trending rail: globally popular recent posts, used as the discovery fallback."""
from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from ...models import Post
from ...services.feed_helpers import (
    post_visibility_q,
    likes_count_subquery, comments_count_subquery, saves_count_subquery,
)
from ..constants import TRENDING_HALF_LIFE_DAYS, TRENDING_POOL, TRENDING_TTL_S, TRENDING_WINDOW_DAYS
from ..scoring import _annotate_for_serialize, _engagement_score, _exclude_not_interested, recency_decay_days

# =============================================================================
# Global-trending fallback (final resort when every rail's chain is exhausted)
# =============================================================================

def _rail_trending(request, user, context, *, limit: int, exclude_ids: set[int]):
    """
    Pure engagement + recency, globally cached for TRENDING_TTL_S. No
    personalization — this exists to keep slots filled when every rail's
    fallback chain is exhausted.
    """
    cache_key = "feed:trending:global"
    scored = cache.get(cache_key)
    if scored is None:
        candidates = (
            Post.objects
            .filter(
                created_at__gte=timezone.now() - timedelta(days=TRENDING_WINDOW_DAYS)
            )
            .annotate(
                likes_count_g=likes_count_subquery(),
                comments_count_g=comments_count_subquery(),
                saves_count_g=saves_count_subquery(),
            )
            .order_by("-created_at")[:TRENDING_POOL * 3]
        )
        scored_pairs = []
        for p in candidates:
            engagement = _engagement_score(
                p.likes_count_g, p.comments_count_g, p.saves_count_g,
                p.impression_count,
            )
            score = engagement * recency_decay_days(p.created_at, TRENDING_HALF_LIFE_DAYS)
            scored_pairs.append((p.id, score))
        scored_pairs.sort(key=lambda x: x[1], reverse=True)
        scored = scored_pairs[:TRENDING_POOL]
        cache.set(cache_key, scored, timeout=TRENDING_TTL_S)

    # Apply per-viewer exclusions at fetch time. The cache holds a global
    # pool scored purely on engagement + recency, so it MUST be filtered
    # per viewer here — otherwise a post from a private page that happens
    # to be popular among its small follower set will surface in trending
    # for everyone whose author isn't blocked/muted. The FEED_RANKING_SPEC
    # at docs/FEED_RANKING_SPEC.md:300 makes this explicit:
    #
    #     "Every rail reuses build_feed_context()'s exclusion sets... No
    #      rail may build its own exclusions — drift between rails is how
    #      a blocked user's post ends up in front of you."
    #
    # The previous version only excluded blocked authors and stopped at
    # `limit` candidates before re-querying — private-page posts whose
    # authors weren't blocked passed through silently.
    #
    # Implementation: walk the entire cached pool (bounded by
    # TRENDING_POOL) to gather candidate post_ids; do ONE bulk fetch with
    # post_visibility_q applied at the DB layer; preserve the cache's
    # engagement order; truncate to `limit` AFTER visibility filtering, so
    # privacy drops don't leave fewer-than-expected slots.
    #
    # The earlier Python `if pid in context["blocked_user_ids"]` check
    # compared post_ids against user_ids (different ID spaces) — a no-op
    # in practice, since the DB exclude on user_id below was the real
    # gate. It's removed here for clarity.
    candidate_ids = []
    for pid, _score in scored:
        if pid in exclude_ids:
            continue
        candidate_ids.append(pid)
    if not candidate_ids:
        return []

    trending_qs = (
        _annotate_for_serialize(
            Post.objects.filter(id__in=candidate_ids),
            user,
        )
        .filter(post_visibility_q(
            user, context["followed_users"], context["followed_pages"],
        ))
        .exclude(user_id__in=context["blocked_user_ids"])
        .exclude(user_id__in=context["muted_user_ids"])
        .exclude(page_id__in=context["muted_page_ids"])
        .distinct()
    )
    # Honour the viewer's "not interested" choices even though trending is a
    # globally-cached pool (B2).
    trending_qs = _exclude_not_interested(trending_qs, context)
    posts = list(trending_qs)
    post_map = {p.id: p for p in posts}
    score_map = dict(scored)

    # Preserve the cache's engagement order; truncate to `limit` AFTER
    # visibility filtering, so dropped-private posts don't leave holes.
    out = []
    for pid in candidate_ids:
        if pid not in post_map:
            continue
        out.append((pid, score_map.get(pid, 0.0), post_map[pid]))
        if len(out) >= limit:
            break
    return out
