"""Per-viewer activity/affinity profile: build, compute, normalise, store."""
from __future__ import annotations
import logging

import math
from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from ..models import Activity, Post, PostHashtag, UserAffinityProfile
from .constants import ACTIVITY_PROFILE_TTL_S, ACTIVITY_WINDOW_DAYS

logger = logging.getLogger(__name__)

# =============================================================================
# RAIL (d) — Activity-based
# =============================================================================
#
# Two-phase: first build a per-viewer affinity profile (cached), then score
# candidates against it. The profile is the only piece that examines the
# Activity table; the scoring phase uses cheap dict lookups.

# Per-action_type point values. Positive = signal of taste; negative = anti-signal.
# Values picked to match the spec; tune via constants above if needed.
#
# `post_impression` and `post_dwell` are intentionally absent: an impression
# is not engagement (it's the denominator for CTR-style ranking, not a
# positive weight), and an in-feed dwell on its own is a weak signal that
# would otherwise dominate the profile by sheer volume. If you want to
# fold them in later, do it as a small fractional weight (≤ 0.5) and
# revisit the cold-start threshold so they don't push noisy authors into
# the candidate pool.
_ACTION_POINTS = {
    "post_save":     5.0,
    "post_like":     3.0,
    "post_share":    4.0,
    "post_comment":  3.0,   # baseline; sentiment-adjusted below
    # D1 — reading deep into a post's comments. Independent of liking; scaled
    # by the depth fraction the client reports (see the comment_scroll block
    # in the scoring loop), so this 3.0 is the credit for a near-complete read
    # and shallow glances are dropped before they reach here.
    "comment_scroll": 3.0,
    "reel_rewatch":  4.0,
    "reel_complete": 3.0,
    "reel_watch":    2.0,   # scaled by watch-duration ratio (see below)
    "post_view":     2.0,   # only if duration >= 8s
    "user_visit":    2.0,
    "page_visit":    1.0,
    "hashtag_engage": 3.0,
    "search_query":  3.0,   # query text mined for niche/hashtag keywords
    "search_click":  1.5,
    "reel_skip":    -3.0,
    "post_unlike":  -3.0,
    "post_unsave":  -3.0,
    # Explicit "not interested" (B2). Stronger than an unlike because it's a
    # deliberate rejection. Flows through the existing attribution: an
    # author-kind row carries target_user_id (subtracts from author affinity),
    # a topic-kind row carries hashtag (subtracts from hashtag affinity). The
    # hard exclusion in build_feed_context is the durable mechanism; this
    # negative weight just keeps the affinity profile honest as it decays.
    "not_interested": -8.0,
}


_AFFINITY_DECAY_HALF_LIFE_DAYS = 14.0


# ── B1 signal-tuning constants ───────────────────────────────────────────────
# (ACTIVITY_AND_FEED_AUDIT.md item B1 — "plug in four signals you already
#  collect but don't use".)

# Signal 3 — reel watch duration. reel_watch carries the (foreground-correct,
# post-A4) wall-clock watch time, and now also the video's own length in
# metadata["video_seconds"]. We score the watch as a COMPLETION RATIO
# (watch / length, capped at 1.0): 28 s of a 30 s clip ≈ 0.93 (hooked),
# 28 s of a 5-minute video ≈ 0.09 (a glance) — which a fixed reference
# couldn't distinguish. _REEL_WATCH_FULL_SECONDS is now only a FALLBACK
# reference for legacy rows logged before the client started sending the
# video length. _REEL_WATCH_MIN_SECONDS is an absolute floor: watches below
# it are dropped (reel_skip captures genuine skips), which also stops a
# trivially short clip from earning full credit off a 1-2 s autoplay.
_REEL_WATCH_FULL_SECONDS = 30.0   # fallback only; real length preferred

_REEL_WATCH_MIN_SECONDS = 3.0


# Signal 4 — credit a post's OWN hashtags (from the PostHashtag index, A8)
# when the viewer engages with the post (save/like/share/qualifying view/
# reel events). A fixed budget of the event weight is split evenly across
# the post's tags, so a focused 1-tag post gives that tag a strong signal
# while a 6-tag post can't swamp the profile. Author attribution stays the
# dominant signal (it gets the full weight; tags share this fraction).
_POST_CONTENT_HASHTAG_BUDGET = 0.6


# D1 — comment scroll depth. The client reports how far down the loaded
# comment thread the viewer scrolled, as a fraction in [0, 1]. We drop
# shallow glances below this floor (just opening the sheet and reading the
# first comment or two is noise, not interest) and scale the event weight by
# the depth so a near-complete read counts strongly and a half-scroll counts
# proportionally. The credit flows through the SAME author + Signal-4 hashtag
# attribution as any other post-keyed event, because a comment_scroll row
# carries the post_id.
_COMMENT_SCROLL_MIN_DEPTH = 0.25


# Signal 1 — discovery appetite from tab dwell. Each tab_view contributes its
# duration (or a nominal value when the client didn't send one), capped so a
# single marathon session can't dominate. We only trust the ratio once we've
# seen at least _DISCOVERY_MIN_SAMPLES tab views; below that, appetite is
# None → the default slot layout is used.
_TAB_VIEW_DEFAULT_SECONDS = 5.0

_TAB_VIEW_CAP_SECONDS = 300.0

_DISCOVERY_MIN_SAMPLES = 5

# The tab(s) that represent "discovery" intent. Search is the discovery
# surface in this app (Explore lives inside it).
_DISCOVERY_TABS = ("Search",)



def _normalize_profile(data: dict) -> dict:
    """
    Restore the integer dict keys that a JSON round-trip stringifies, so a
    profile read back from UserAffinityProfile.data has the SAME shape as one
    fresh from _compute_affinity_profile. Author ids and time-of-day bucket
    ids are ints; hashtag / keyword keys are genuinely strings and stay as-is.
    """
    def _int_keys(d):
        out = {}
        for k, v in (d or {}).items():
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                out[k] = v
        return out

    data["author"] = _int_keys(data.get("author"))
    data["author_tod"] = {
        int(b): _int_keys(m) for b, m in (data.get("author_tod") or {}).items()
    }
    data["hashtag_tod"] = {
        int(b): m for b, m in (data.get("hashtag_tod") or {}).items()
    }
    # Defensive defaults so a sparse/old row never KeyErrors downstream.
    data.setdefault("hashtag", {})
    data.setdefault("keyword", {})
    data.setdefault("n_events", 0)
    data.setdefault("discovery_appetite", None)
    return data



def _store_affinity_profile(user, profile):
    """Upsert a computed profile into UserAffinityProfile (best-effort)."""
    try:
        UserAffinityProfile.objects.update_or_create(
            user=user, defaults={"data": profile}
        )
    except Exception as exc:   # pragma: no cover — persistence is best-effort
        logger.error(f"[_store_affinity_profile] failed for "
              f"{getattr(user, 'id', None)}: {exc}")



def _build_activity_profile(user) -> dict:
    """
    Return the viewer's affinity profile (author / hashtag / keyword affinity,
    per-time-of-day author & hashtag affinity, n_events, discovery_appetite).

    Read path only (C4). The expensive 30-day Activity scan moved to the
    nightly `build_affinity_profiles` job, which writes each user's profile to
    the UserAffinityProfile table. Here we:

        1. return the per-process cache if warm;
        2. else read the precomputed row — one indexed lookup, no scan;
        3. else (no row yet: brand-new user, or before the first nightly run)
           compute on demand AS A FALLBACK, persist it so the next read is
           cheap, and cache it.

    So an established user never triggers the 30-day scan on a request — only
    users not yet in the table do, and they have little activity to scan. A
    failed/late overnight job degrades freshness, never request latency.
    """
    cache_key = f"feed:activity_profile:{user.id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    row = (
        UserAffinityProfile.objects
        .filter(user=user)
        .values_list("data", flat=True)
        .first()
    )
    if row:
        try:
            profile = _normalize_profile(dict(row))
            cache.set(cache_key, profile, timeout=ACTIVITY_PROFILE_TTL_S)
            return profile
        except Exception as exc:   # corrupt row → fall through to recompute
            logger.warning(f"[_build_activity_profile] bad stored profile for "
                  f"{user.id}: {exc}")

    # Fallback: no usable precomputed row. Compute now (cheap for a
    # low-activity new user), persist for next time, cache.
    profile = _compute_affinity_profile(user)
    _store_affinity_profile(user, profile)
    cache.set(cache_key, profile, timeout=ACTIVITY_PROFILE_TTL_S)
    return profile



def _compute_affinity_profile(user) -> dict:
    """
    Compute the viewer affinity profile from the last 30 days of Activity.

    The expensive part (this function) runs in the nightly job for established
    users; on the request path it's only the fallback for users with no row
    yet. Returns the profile dict — no caching / persistence (callers handle
    that). Time-decayed exponentially over the window.
    """
    since = timezone.now() - timedelta(days=ACTIVITY_WINDOW_DAYS)
    # Materialize the queryset once and reuse — `values()` returns a fresh
    # iterator that, when consumed, requires a re-query to iterate again.
    # The previous version scanned the Activity table twice per cache miss
    # (once to build touched_post_ids, once for the main loop).
    acts = list(
        Activity.objects
        .filter(user=user, created_at__gte=since)
        .values(
            "action_type", "post_id", "page_id", "target_user_id",
            "duration_seconds", "hashtag", "sentiment_label", "keywords",
            "query", "tab", "metadata", "created_at",
        )
    )

    # extract_post_keywords mines niche:/intent:/hashtag: tags from free text
    # — reused here to turn a search query into the SAME vocabulary the
    # candidate posts expose, so search intent (Signal 2) actually matches.
    from ..services.comment_analyzer import extract_post_keywords

    author_aff: dict[int, float] = defaultdict(float)
    hashtag_aff: dict[str, float] = defaultdict(float)
    keyword_aff: dict[str, float] = defaultdict(float)

    # Time-of-day affinity (B6): the same author/hashtag credit, but bucketed
    # by the 6-hour slice of the day each event happened in (UTC — see the
    # loop). Used at scoring time to boost content that matches what the
    # viewer engages with around *this* time of day. Keyed
    # {bucket: {id_or_tag: score}}.
    author_aff_tod: dict[int, dict] = defaultdict(lambda: defaultdict(float))
    hashtag_aff_tod: dict[int, dict] = defaultdict(lambda: defaultdict(float))

    def _credit_author(aid, w, bucket):
        author_aff[aid] += w
        author_aff_tod[bucket][aid] += w

    def _credit_hashtag(tag, w, bucket):
        hashtag_aff[tag] += w
        hashtag_aff_tod[bucket][tag] += w

    # Pre-load author_id for any post_id touched by activities so we can
    # attribute post-level events to their author. One batch query.
    touched_post_ids = {
        a["post_id"] for a in acts if a["post_id"] is not None
    }
    post_author_map: dict[int, int] = {}
    post_hashtags_map: dict[int, list[str]] = defaultdict(list)
    if touched_post_ids:
        post_author_map = dict(
            Post.objects.filter(id__in=touched_post_ids)
            .values_list("id", "user_id")
        )
        # Signal 4: load the hashtags of every engaged-with post from the
        # PostHashtag index (A8) in one query, so a save/like/share credits
        # the post's TOPICS, not just its author. defaultdict(list) means a
        # post with no tags simply contributes nothing here.
        for pid, tag in (
            PostHashtag.objects
            .filter(post_id__in=touched_post_ids)
            .values_list("post_id", "hashtag")
        ):
            post_hashtags_map[pid].append(tag)

    # ── PAGE-VISIT AUTHOR FAN-OUT ──────────────────────────────────
    # Spec (docs/FEED_RANKING_SPEC.md:183): when the viewer visits a
    # page, credit ALL authors who've posted in that page recently —
    # the idea being "you cared enough to look at this page; you
    # probably want to see more from people who post there."
    #
    # The previous code only credited a["target_user_id"], which is
    # always null on page_visit Activity rows (page visits carry a
    # page_id, not a target_user_id). So the +1 weight evaporated.
    #
    # Strategy: collect every visited page_id, do ONE batch query for
    # posts in those pages over the last 14 days, then group in Python
    # and cap each page at the 10 most-recent distinct authors. The
    # outer slice (5000) bounds the worst case where one page has
    # thousands of recent posts.
    page_visit_page_ids = {
        a["page_id"] for a in acts
        if a["action_type"] == "page_visit" and a["page_id"] is not None
    }
    page_recent_authors_map: dict[int, list[int]] = {}
    if page_visit_page_ids:
        recent_since = timezone.now() - timedelta(days=14)
        rows = list(
            Post.objects
            .filter(
                page_id__in=page_visit_page_ids,
                created_at__gte=recent_since,
            )
            .order_by("-created_at")
            .values_list("page_id", "user_id")[:5000]
        )
        for page_id, user_id in rows:
            bucket = page_recent_authors_map.setdefault(page_id, [])
            if len(bucket) >= 10:
                continue
            if user_id not in bucket:
                bucket.append(user_id)

    n_events = 0
    now = timezone.now()

    # Signal 1 accumulators — tab dwell, used for discovery appetite below.
    tab_weight: dict[str, float] = defaultdict(float)
    tab_view_count = 0

    for a in acts:
        n_events += 1
        action = a["action_type"]

        # ── Signal 1: tab dwell ─────────────────────────────────────────
        # tab_view rows carry no affinity of their own; we aggregate their
        # dwell per tab to derive how much the viewer leans toward discovery
        # surfaces (Search) vs. their Home feed. Handled before the
        # _ACTION_POINTS lookup because tab_view intentionally isn't scored.
        if action == "tab_view":
            tab_view_count += 1
            tw = a["duration_seconds"]
            tw = _TAB_VIEW_DEFAULT_SECONDS if tw is None else tw
            tw = min(max(tw, 0.0), _TAB_VIEW_CAP_SECONDS)
            tab_weight[(a["tab"] or "").strip()] += tw
            continue

        base = _ACTION_POINTS.get(action)
        if base is None:
            continue

        # Action-specific gating / adjustments.
        if action == "post_view":
            dur = a["duration_seconds"] or 0
            if dur < 8:
                continue
        if action == "post_comment":
            # Tone down negative-sentiment comments.
            if a["sentiment_label"] == "negative":
                base = 1.0

        # ── Signal 3: reel watch duration → completion ratio ────────────
        # Score the watch by how much of THIS video was actually seen:
        # watch_time / video_length, capped at 1.0. That self-adjusts to
        # every clip — 28 s of a 30 s reel is deep engagement (~0.93), 28 s
        # of a 5-minute video is a glance (~0.09) — which the old fixed
        # 30 s reference couldn't tell apart. The video length rides in
        # metadata["video_seconds"] (sent by the client post-fix); rows
        # without it fall back to the fixed reference. Watches under the
        # absolute floor are dropped as grazes (reel_skip covers real skips,
        # and the floor stops a 1-2 s autoplay of a tiny clip scoring 1.0).
        reel_scale = 1.0
        if action == "reel_watch":
            rdur = a["duration_seconds"]
            if rdur is None or rdur < _REEL_WATCH_MIN_SECONDS:
                continue
            meta = a["metadata"] if isinstance(a["metadata"], dict) else {}
            vid_len = meta.get("video_seconds")
            if isinstance(vid_len, (int, float)) and vid_len > 0:
                reel_scale = min(rdur / vid_len, 1.0)        # true ratio
            else:
                reel_scale = min(rdur / _REEL_WATCH_FULL_SECONDS, 1.0)  # fallback

        # Duration-based gate + scale for visit actions. Both `page_visit`
        # and `user_visit` carry a duration_seconds field that previously
        # went unused for affinity — meaning a 2-second accidental tap
        # counted the same as a 90-second genuine browse. We now:
        #   • Drop visits under 3 seconds entirely (noise / mis-tap).
        #   • Linearly scale 3–30 seconds from ~10% → 100% of base weight.
        #   • Cap at 30 seconds — longer visits don't earn more credit
        #     than a healthy "browsed the page" baseline.
        #   • Missing duration (defensive: older rows may lack it) leaves
        #     the base weight untouched rather than dropping the signal.
        # Same shape as the existing `post_view` gate at line ~702; we
        # add the linear ramp because visits are more variable than feed
        # views and a binary cliff under-weights short-but-real interest.
        visit_scale = 1.0
        if action in ("page_visit", "user_visit"):
            visit_dur = a["duration_seconds"]
            if visit_dur is not None:
                if visit_dur < 3:
                    continue
                visit_scale = min(visit_dur / 30.0, 1.0)

        # ── D1: comment scroll depth → interest scale ───────────────────
        # comment_scroll rows carry a depth fraction in metadata["depth"]
        # (0..1 = how far down the loaded thread the viewer scrolled). Drop
        # shallow glances below the floor as noise; scale the rest by depth.
        # The post_id attribution below then credits the post's author and
        # (via Signal 4) its hashtags — so deep-reading a post's comments
        # builds taste for that post's author and topics without the viewer
        # having to like it.
        cscroll_scale = 1.0
        if action == "comment_scroll":
            meta = a["metadata"] if isinstance(a["metadata"], dict) else {}
            depth = meta.get("depth")
            if not isinstance(depth, (int, float)):
                continue
            depth = min(max(float(depth), 0.0), 1.0)
            if depth < _COMMENT_SCROLL_MIN_DEPTH:
                continue
            cscroll_scale = depth

        age_days = (now - a["created_at"]).total_seconds() / 86400.0
        decay = math.exp(-age_days / _AFFINITY_DECAY_HALF_LIFE_DAYS)
        weight = base * decay * visit_scale * reel_scale * cscroll_scale

        # Time-of-day bucket for this event (B6). 6-hour slices in UTC. We use
        # UTC for both the historical events and "now" at scoring time, so the
        # pattern is self-consistent per user without needing their timezone:
        # a viewer's "morning" events and a "morning" feed load fall in the
        # same bucket as long as their local offset is roughly constant.
        bucket = a["created_at"].hour // 6

        # ── Signal 2: search history → keyword / hashtag affinity ───────
        # A search query is a strong intent signal that was previously
        # ignored entirely. Run the query through extract_post_keywords so
        # it produces the same niche:/intent:/hashtag: vocabulary the
        # candidate posts expose, then fold it into the affinity dicts.
        # search_query rows have no post/author/hashtag-field of their own,
        # so we attribute here and skip the generic blocks below.
        if action == "search_query":
            q = (a["query"] or "").strip()
            if q:
                for kw in extract_post_keywords(q):
                    if kw.startswith("hashtag:"):
                        _credit_hashtag(kw.split(":", 1)[1], weight, bucket)
                    else:
                        keyword_aff[kw] += weight
            continue

        # Author attribution.
        post_id = a["post_id"]
        if post_id and post_id in post_author_map:
            _credit_author(post_author_map[post_id], weight, bucket)

        if a["target_user_id"]:
            # user_visit lands here (target_user_id is the visited user).
            # The credit picks up the duration-scaled weight from above.
            _credit_author(a["target_user_id"], weight, bucket)

        # page_visit fan-out. Credit each of the visited page's recent
        # authors (capped at 10) so the spec's "credit all authors who
        # posted in that page recently" rule actually has effect. Without
        # this, page_visit's +1 base weight is silently dropped (the
        # target_user_id branch above can't fire because page visits
        # don't carry a target user).
        if action == "page_visit":
            page_id = a["page_id"]
            if page_id is not None:
                for author_id in page_recent_authors_map.get(page_id, ()):
                    _credit_author(author_id, weight, bucket)

        # ── Signal 4: the engaged-with post's OWN hashtags ──────────────
        # A save/like/share/qualifying-view of a post should build affinity
        # for that post's TOPICS, not just its author. We split a fixed
        # budget of the event weight evenly across the post's tags, so a
        # focused single-tag post gives a strong per-tag signal while a
        # tag-stuffed post can't swamp the profile. Because the event weight
        # already differentiates a save (5.0) from a like (3.0), saves
        # naturally build more topic affinity — the "saving is more
        # deliberate" property the audit calls out.
        if post_id:
            tags = post_hashtags_map.get(post_id)
            if tags:
                per_tag = (weight * _POST_CONTENT_HASHTAG_BUDGET) / len(tags)
                for tag in tags:
                    _credit_hashtag(tag, per_tag, bucket)

        # Hashtag attribution (explicit hashtag field — hashtag_engage rows).
        h = (a["hashtag"] or "").strip().lower()
        if h:
            _credit_hashtag(h, weight, bucket)

        # Keyword attribution (free-form list on Activity).
        for kw in (a["keywords"] or [])[:10]:   # cap per-event to avoid spam
            if not isinstance(kw, str):
                continue
            kw_norm = kw.strip().lower()
            if kw_norm:
                keyword_aff[kw_norm] += weight

    # ── Signal 1: derive discovery appetite from the tab dwell totals ───
    # Fraction of tab time the viewer spends on discovery surfaces (Search)
    # vs. everywhere else. None until we have a minimum sample, so cold-start
    # users fall back to the default slot layout rather than being typed off
    # one or two taps.
    discovery_appetite = None
    total_tab_weight = sum(tab_weight.values())
    if tab_view_count >= _DISCOVERY_MIN_SAMPLES and total_tab_weight > 0:
        discovery_weight = sum(tab_weight.get(t, 0.0) for t in _DISCOVERY_TABS)
        discovery_appetite = discovery_weight / total_tab_weight

    def _top_n(d: dict, n: int) -> dict:
        return dict(sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:n])

    profile = {
        "author":   _top_n(author_aff, 200),
        "hashtag":  _top_n(hashtag_aff, 50),
        "keyword":  _top_n(keyword_aff, 50),
        # Time-of-day affinity (B6): per-bucket top authors/hashtags. Capped
        # tighter than the global maps (the boost is a nudge, not the main
        # signal) to keep the cached profile small. Bucket keys are 0..3.
        "author_tod":  {b: _top_n(d, 50) for b, d in author_aff_tod.items()},
        "hashtag_tod": {b: _top_n(d, 20) for b, d in hashtag_aff_tod.items()},
        "n_events": n_events,
        # Signal 1: discovery appetite (0..1, or None when undeterminable).
        # Consumed by compose_home_feed_page to pick the slot layout.
        "discovery_appetite": discovery_appetite,
    }
    return profile
