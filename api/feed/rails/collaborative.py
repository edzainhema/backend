"""Collaborative ('people like you') rail: recent posts from precomputed recommended authors."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from ...models import Post, RecommendedAuthor
from ...services.feed_helpers import post_visibility_q
from ..constants import COLLABORATIVE_HALF_LIFE_DAYS, COLLABORATIVE_MAX_PER_AUTHOR, COLLABORATIVE_POOL, COLLABORATIVE_TOP_AUTHORS, COLLABORATIVE_TTL_S, COLLABORATIVE_WINDOW_DAYS
from ..scoring import _annotate_for_serialize, _engagement_score, _exclude_not_interested, recency_decay_days

# =============================================================================
# RAIL (e) — Collaborative ("people like you")
# =============================================================================
#
# Reads the precomputed RecommendedAuthor table (built nightly by the
# build_collaborative_recs management command) and surfaces recent posts from
# the viewer's recommended authors. This is the only rail that can recommend
# a genuinely fresh author the viewer has never touched — see
# ACTIVITY_AND_FEED_AUDIT.md item B3. All the heavy similarity math runs
# offline; here it's a cheap indexed read + the same scoring shape as the
# other rails.
#
# Cold start: a viewer with no recommendations yet (new user, or before the
# first nightly run) gets an empty list, and the slot falls back via
# FALLBACK_ORDER — so the feature is safe to ship before the job ever runs.

def _rail_collaborative(request, user, context, *, offset: int, limit: int,
                        exclude_ids: set[int]):
    cache_key = f"feed:collaborative:{user.id}"
    scored = cache.get(cache_key)

    if scored is None:
        author_scores = dict(
            RecommendedAuthor.objects
            .filter(user=user)
            .order_by("-score")
            .values_list("author_id", "score")[:COLLABORATIVE_TOP_AUTHORS]
        )
        if not author_scores:
            return []

        candidates = (
            Post.objects
            .filter(post_visibility_q(
                user, context["followed_users"], context["followed_pages"],
            ))
            .filter(user_id__in=author_scores.keys())
            .exclude(user_id__in=context["followed_users"])
            .exclude(user_id=user.id)
            .exclude(user_id__in=context["blocked_user_ids"])
            .exclude(user_id__in=context["muted_user_ids"])
            .exclude(page_id__in=context["muted_page_ids"])
            .filter(
                created_at__gte=timezone.now() - timedelta(days=COLLABORATIVE_WINDOW_DAYS)
            )
            .distinct()
        )
        candidates = _exclude_not_interested(candidates, context)
        candidates = _annotate_for_serialize(candidates, user).order_by("-created_at")[:COLLABORATIVE_POOL]
        candidates = list(candidates)

        # Diversity cap — keep at most COLLABORATIVE_MAX_PER_AUTHOR posts from
        # any single recommended author so the rail spreads across the people
        # CF surfaced rather than dumping one hot author's whole week.
        per_author = defaultdict(int)
        deduped = []
        for p in candidates:
            if per_author[p.user_id] >= COLLABORATIVE_MAX_PER_AUTHOR:
                continue
            per_author[p.user_id] += 1
            deduped.append(p)
        candidates = deduped

        scored_pairs = []
        for p in candidates:
            rec = author_scores.get(p.user_id, 0.0)
            engagement = _engagement_score(
                p.likes_count_ann, p.comments_count_ann, p.saves_count_ann,
                p.impression_count,
            )
            score = (
                (rec * 2 + engagement)
                * recency_decay_days(p.created_at, COLLABORATIVE_HALF_LIFE_DAYS)
            )
            scored_pairs.append((p.id, score))

        scored_pairs.sort(key=lambda x: x[1], reverse=True)
        scored = scored_pairs
        cache.set(cache_key, scored, timeout=COLLABORATIVE_TTL_S)

        post_map = {p.id: p for p in candidates}
        out = []
        for pid, score in scored:
            if pid in exclude_ids:
                continue
            if pid not in post_map:
                continue
            out.append((pid, score, post_map[pid]))
            if len(out) >= offset + limit:
                break
        return out[offset:offset + limit]

    # Cache hit.
    wanted_ids = [
        pid for pid, _ in scored
        if pid not in exclude_ids
    ][offset:offset + limit]
    if not wanted_ids:
        return []
    posts = list(
        _annotate_for_serialize(Post.objects.filter(id__in=wanted_ids), user)
    )
    post_map = {p.id: p for p in posts}
    score_map = dict(scored)
    return [
        (pid, score_map[pid], post_map[pid])
        for pid in wanted_ids
        if pid in post_map
    ]
