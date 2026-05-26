"""Activity rail: per-viewer personalised content from author/hashtag affinity, with a live-trending-hashtag boost."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from ...models import Post, PostHashtag
from ...services.feed_helpers import post_visibility_q
from ..affinity import _build_activity_profile
from ..constants import ACTIVITY_COLD_START_EVENTS, ACTIVITY_COLD_START_MIN_SCALE, ACTIVITY_HALF_LIFE_DAYS, ACTIVITY_MAX_PER_AUTHOR, ACTIVITY_MIN_CONTENT, ACTIVITY_MIN_SCORE, ACTIVITY_POOL, ACTIVITY_SCORES_TTL_S, ACTIVITY_TOD_BOOST, ACTIVITY_WINDOW_DAYS, TRENDING_HASHTAG_BOOST
from ..scoring import _annotate_for_serialize, _engagement_score, _exclude_not_interested, recency_decay_days
from ..trending import get_trending_hashtags

def _rail_activity(request, user, context, *, offset: int, limit: int,
                   exclude_ids: set[int]):
    """
    Returns [(post_id, score, post_obj), ...] for posts ranked by the
    viewer's affinity profile.

    Cold start ramps smoothly rather than hard-cutting at 20 events (B7):
    personalization strength = n_events/20 (capped at 1.0), and that strength
    scales the quality thresholds so newer viewers' weaker matches are let
    through proportionally. Below ACTIVITY_COLD_START_MIN_SCALE there's too
    little signal and the rail stays empty.

    Scoring also gets a time-of-day boost (B6): content matching the viewer's
    interests in the current 6-hour slice of day ranks a little higher.
    """
    profile = _build_activity_profile(user)
    cold_start_scale = min(profile["n_events"] / ACTIVITY_COLD_START_EVENTS, 1.0)
    if cold_start_scale < ACTIVITY_COLD_START_MIN_SCALE:
        return []
    effective_min_content = ACTIVITY_MIN_CONTENT * cold_start_scale
    effective_min_score = ACTIVITY_MIN_SCORE * cold_start_scale

    # Time-of-day bucket for "now" (UTC, same clock used when bucketing the
    # historical events in _build_activity_profile). The scored list is cached
    # per bucket so crossing a 6-hour boundary picks up the right slice
    # instead of serving the previous slice's ranking for up to the TTL.
    current_bucket = timezone.now().hour // 6
    cache_key = f"feed:activity_scores:{user.id}:{current_bucket}"
    scored = cache.get(cache_key)

    author_aff = profile["author"]
    hashtag_aff = profile["hashtag"]
    keyword_aff = profile["keyword"]
    tod_author = profile.get("author_tod", {}).get(current_bucket, {})
    tod_hashtag = profile.get("hashtag_tod", {}).get(current_bucket, {})

    if scored is None:
        top_authors = list(author_aff.keys())[:50]

        # Match candidate posts by hashtag via the denormalized PostHashtag
        # index — an EXACT `hashtag__in=[...]` lookup, not the old
        # `description__icontains="#blue"` substring scan. The substring
        # form matched "#blueberry"/"#bluetooth" for a viewer who liked
        # "#blue", and ran as an unindexed full-text scan. The covering
        # index on PostHashtag(hashtag, post) makes this a fast indexed
        # probe and only ever matches whole tags. See ACTIVITY_AND_FEED_AUDIT.md
        # item A8. Cap to the top 20 hashtags so the IN-list stays bounded.
        top_hashtags = list(hashtag_aff.keys())[:20]

        match_q = Q()
        if top_authors:
            match_q |= Q(user_id__in=top_authors)
        if top_hashtags:
            hashtag_post_ids = (
                PostHashtag.objects
                .filter(hashtag__in=top_hashtags)
                .values_list("post_id", flat=True)
            )
            match_q |= Q(id__in=hashtag_post_ids)

        if not match_q:
            # No signal at all — leave rail empty, let fallback fill the slot.
            return []

        candidates = (
            Post.objects
            .filter(post_visibility_q(
                user, context["followed_users"], context["followed_pages"],
            ))
            .exclude(user_id__in=context["followed_users"])
            .exclude(user_id=user.id)
            .exclude(user_id__in=context["blocked_user_ids"])
            .exclude(user_id__in=context["muted_user_ids"])
            .exclude(page_id__in=context["muted_page_ids"])
            .filter(
                created_at__gte=timezone.now() - timedelta(days=ACTIVITY_WINDOW_DAYS)
            )
            .filter(match_q)
            .distinct()
        )
        candidates = _exclude_not_interested(candidates, context)
        candidates = _annotate_for_serialize(candidates, user).order_by("-created_at")[:ACTIVITY_POOL]
        candidates = list(candidates)

        from ...comment_analyzer import extract_post_keywords

        # Prefetch the candidate posts' hashtags from the index in ONE
        # query, instead of re-running the extraction regex on every
        # candidate description. This both speeds up scoring and keeps the
        # tags used for scoring identical to the tags that were matched.
        candidate_ids = [p.id for p in candidates]
        post_hashtags_map: dict[int, list[str]] = defaultdict(list)
        if candidate_ids:
            for pid, tag in (
                PostHashtag.objects
                .filter(post_id__in=candidate_ids)
                .values_list("post_id", "hashtag")
            ):
                post_hashtags_map[pid].append(tag)

        # D2: live trending-hashtag intensities {tag: 0..1}, fetched once per
        # rebuild. Used just below to time-weight a viewer's hashtag affinity —
        # a tag the viewer already likes counts harder while it's trending. An
        # empty map (cron not run / nothing trending) yields a x1.0 multiplier,
        # i.e. exactly the pre-D2 behaviour.
        trending_hashtags = get_trending_hashtags()

        scored_pairs = []
        for p in candidates:
            author_score = author_aff.get(p.user_id, 0.0)

            post_hashtags = post_hashtags_map.get(p.id, ())
            # Each tag's affinity contribution is amplified by (1 + BOOST *
            # intensity) when it's trending right now (D2). A non-trending tag
            # has intensity 0 → multiplier 1.0 → unchanged.
            hashtag_score = sum(
                hashtag_aff.get(h, 0.0)
                * (1.0 + TRENDING_HASHTAG_BOOST * trending_hashtags.get(h, 0.0))
                for h in post_hashtags
            )

            # Keyword affinity. The viewer's keyword_aff dict is populated
            # mostly from comment-derived niche/intent/hashtag tags (see
            # _build_activity_profile above + comment_analyzer.analyze_comment).
            # Until now this was hard-coded to 0.0 because posts had no
            # equivalent tag vocabulary — the spec describes the dimension
            # but it had no matcher. extract_post_keywords now produces the
            # same shape ("niche:fitness", "intent:purchase", "hashtag:X")
            # from a post's description, so an overlap-sum gives real signal.
            # Computed inline rather than denormalized onto Post.keywords
            # (which would need a migration + backfill) — cheap because the
            # candidate pool is bounded by ACTIVITY_POOL.
            post_keywords = extract_post_keywords(p.description or "")
            keyword_score = sum(keyword_aff.get(k, 0.0) for k in post_keywords)

            # Time-of-day boost (B6): extra credit when the author/topic is
            # one the viewer engages with around THIS time of day. Additive
            # and modest (ACTIVITY_TOD_BOOST) so it nudges ranking toward
            # time-appropriate content without overriding all-day taste.
            tod_match = (
                tod_author.get(p.user_id, 0.0)
                + 0.5 * sum(tod_hashtag.get(h, 0.0) for h in post_hashtags)
            )

            content_match = (
                author_score + 0.5 * hashtag_score + 0.3 * keyword_score
                + ACTIVITY_TOD_BOOST * tod_match
            )
            if content_match < effective_min_content:
                continue

            engagement = _engagement_score(
                p.likes_count_ann, p.comments_count_ann, p.saves_count_ann,
                p.impression_count,
            )
            score = (
                (content_match * 2 + engagement)
                * recency_decay_days(p.created_at, ACTIVITY_HALF_LIFE_DAYS)
            )
            if score < effective_min_score:
                continue

            scored_pairs.append((p.id, score))

        scored_pairs.sort(key=lambda x: x[1], reverse=True)

        # Diversity cap (B4): keep at most ACTIVITY_MAX_PER_AUTHOR posts from
        # any single author so one prolific favorite can't fill the rail. Done
        # AFTER ranking so we keep each author's strongest posts (not just
        # their most recent), and baked into the cached `scored` list so the
        # cap holds across every paginated page, not just the first.
        author_of = {p.id: p.user_id for p in candidates}
        per_author: dict[int, int] = defaultdict(int)
        capped_pairs = []
        for pid, score in scored_pairs:
            a = author_of.get(pid)
            if a is not None:
                if per_author[a] >= ACTIVITY_MAX_PER_AUTHOR:
                    continue
                per_author[a] += 1
            capped_pairs.append((pid, score))

        scored = capped_pairs
        cache.set(cache_key, scored, timeout=ACTIVITY_SCORES_TTL_S)

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
