"""Nearby rail: recent, engaging posts within a radius of the viewer's last known location. Includes the viewer-location lookup it depends on."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from ...models import Activity, Post, PostHashtag
from ...services.feed_helpers import post_visibility_q
from ..constants import LOCATION_FRESHNESS_DAYS, NEARBY_HALF_LIFE_DAYS, NEARBY_MIN_ENGAGEMENT, NEARBY_POOL, NEARBY_RADIUS_KM, NEARBY_TTL_S, NEARBY_WINDOW_DAYS
from ..geo import bbox_for_radius, coarse_geohash, haversine_km, nearby_longitude_q
from ..scoring import _annotate_for_serialize, _engagement_log, _engagement_score, _exclude_not_interested, recency_decay_days, rehydrate_visible_slice

# =============================================================================
# RAIL (c) — Nearby
# =============================================================================

def _viewer_location(user):
    """
    Return (lat, lng) if the viewer has a fresh enough device fix, else
    (None, None). "Fresh" = location_updated_at within LOCATION_FRESHNESS_DAYS.
    """
    up = getattr(user, "userprofile", None)
    if up is None:
        return None, None
    if up.latitude is None or up.longitude is None:
        return None, None
    if up.location_updated_at is None:
        return None, None
    age = (timezone.now() - up.location_updated_at).days
    if age > LOCATION_FRESHNESS_DAYS:
        return None, None
    return up.latitude, up.longitude




def _rail_nearby(request, user, context, *, offset: int, limit: int,
                 exclude_ids: set[int]):
    """
    Returns [(post_id, score, post_obj), ...] for posts within
    NEARBY_RADIUS_KM of the viewer's last fix. Rail is disabled (returns [])
    if the viewer has no fresh location.
    """
    viewer_lat, viewer_lng = _viewer_location(user)
    if viewer_lat is None or viewer_lng is None:
        return []

    cache_key = f"feed:nearby:{user.id}:{coarse_geohash(viewer_lat, viewer_lng, 5)}"
    scored = cache.get(cache_key)

    if scored is None:
        min_lat, max_lat, min_lng, max_lng = bbox_for_radius(
            viewer_lat, viewer_lng, NEARBY_RADIUS_KM
        )

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
            .exclude(upload_latitude__isnull=True)
            .exclude(upload_longitude__isnull=True)
            .filter(
                created_at__gte=timezone.now() - timedelta(days=NEARBY_WINDOW_DAYS),
                upload_latitude__gte=min_lat,
                upload_latitude__lte=max_lat,
            )
            # Longitude filtered separately so the prefilter can wrap across
            # the ±180° antimeridian (see nearby_longitude_q / item A13).
            .filter(nearby_longitude_q(min_lng, max_lng))
            .distinct()
        )
        candidates = _exclude_not_interested(candidates, context)
        candidates = _annotate_for_serialize(candidates, user).order_by("-created_at")[:NEARBY_POOL]
        candidates = list(candidates)

        # Small per-viewer activity-affinity terms — these are tiebreakers,
        # not the dominant signal, so we only need cheap lookups.
        recent_visited_authors = set(
            Activity.objects
            .filter(
                user=user,
                action_type__in=("user_visit", "page_visit"),
                created_at__gte=timezone.now() - timedelta(days=14),
            )
            .exclude(target_user__isnull=True)
            .values_list("target_user_id", flat=True)
        )
        recent_engaged_hashtags = set(
            Activity.objects
            .filter(
                user=user,
                action_type="hashtag_engage",
                created_at__gte=timezone.now() - timedelta(days=14),
            )
            .exclude(hashtag="")
            .values_list("hashtag", flat=True)
        )

        # L9: read each candidate's hashtags from the PostHashtag index in ONE
        # query instead of re-running extract_hashtags over every description on
        # the request thread. sync_post_hashtags populates that index via the
        # SAME extract_hashtags (lowercased, de-duped, ≤100 chars — see
        # services/hashtags.py and the PostHashtag model docstring), so the
        # matched tags are identical, and it's the same source the activity rail
        # already scores from. Used only for the boolean "shares a tag with the
        # viewer's recently-engaged hashtags" boost below.
        candidate_ids = [p.id for p in candidates]
        post_hashtags_map: dict[int, list[str]] = defaultdict(list)
        if candidate_ids:
            for pid, tag in (
                PostHashtag.objects
                .filter(post_id__in=candidate_ids)
                .values_list("post_id", "hashtag")
            ):
                post_hashtags_map[pid].append(tag)

        scored_pairs = []
        for p in candidates:
            distance_km = haversine_km(
                viewer_lat, viewer_lng, p.upload_latitude, p.upload_longitude
            )
            if distance_km > NEARBY_RADIUS_KM:
                continue

            # Keep the absolute-engagement floor on raw magnitude (a 2-like
            # post shouldn't clear it just because its rate is high on 2
            # impressions), but score with the CTR-blended value (C5).
            magnitude = _engagement_log(
                p.likes_count_ann, p.comments_count_ann, p.saves_count_ann
            )
            if magnitude < NEARBY_MIN_ENGAGEMENT:
                continue
            engagement = _engagement_score(
                p.likes_count_ann, p.comments_count_ann, p.saves_count_ann,
                p.impression_count,
            )

            proximity = max(0.0, 1.0 - distance_km / NEARBY_RADIUS_KM)

            post_hashtags = post_hashtags_map.get(p.id, ())
            activity_match = 1.0
            if any(h in recent_engaged_hashtags for h in post_hashtags):
                activity_match += 0.5
            if p.user_id in recent_visited_authors:
                activity_match += 0.3

            score = (
                (proximity * 3 + engagement * 2)
                * activity_match
                * recency_decay_days(p.created_at, NEARBY_HALF_LIFE_DAYS)
            )
            scored_pairs.append((p.id, score))

        scored_pairs.sort(key=lambda x: x[1], reverse=True)
        scored = scored_pairs
        cache.set(cache_key, scored, timeout=NEARBY_TTL_S)

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

    # Cache hit — re-apply the per-viewer visibility/block/mute/not-interested
    # gate at fetch time (audit H1). The cached score list was filtered at build
    # time, but it lives for NEARBY_TTL_S and visibility isn't static over that
    # window. Shared helper so the rails can't drift.
    return rehydrate_visible_slice(
        scored, user=user, context=context,
        exclude_ids=exclude_ids, offset=offset, limit=limit,
    )
