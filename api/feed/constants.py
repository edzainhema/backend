"""Tunable constants for the home-feed ranking pipeline. Kept together so the team can tune without code archaeology."""
from __future__ import annotations


# =============================================================================
# CONSTANTS — all in one place so the team can tune without code archaeology
# =============================================================================

PAGE_SIZE = 10


# Slots-per-page allocation. Must sum to PAGE_SIZE.
RAIL_SLOTS = {
    "followed":       6,
    "friend_network": 1,
    "nearby":         1,
    "activity":       2,
}


# Layout of rails inside a page (1-indexed positions mapped to rail name).
# Designed so discovery is scattered, never adjacent, and never first.
SLOT_LAYOUT = [
    "followed",       # 1
    "followed",       # 2
    "followed",       # 3
    "activity",       # 4
    "followed",       # 5
    "followed",       # 6
    "nearby",         # 7
    "collaborative",  # 8  — "people like you" (B3)
    "friend_network", # 9
    "followed",       # 10
]

assert len(SLOT_LAYOUT) == PAGE_SIZE, "SLOT_LAYOUT must be PAGE_SIZE long"


# SLOT_LAYOUT above is the default/fallback mix. The actual per-viewer layout
# is COMPUTED by compute_slot_layout() (B5), which sizes the followed↔discovery
# balance from the viewer's follow count, taste-profile maturity, and discovery
# appetite — see the discovery rails (b)/(c)/(d)/(e) below. The B1 single-signal
# preset picker was generalised into that function.

# Discovery-appetite thresholds (fraction of tab dwell spent on Search). Used
# by compute_slot_layout as a ±1 nudge on top of the follow-count band.
DISCOVERY_LOW_THRESHOLD = 0.15

DISCOVERY_HIGH_THRESHOLD = 0.45



# How the discovery budget is split across the four discovery rails. Priority
# lists of length 6 (the max budget); the first `budget` entries are filled.
#
#   • Mature profile → favour the PERSONALISED rails (activity, collaborative);
#     they're only any good once the viewer has a taste profile / CF recs.
#   • Cold start → those two are empty, so favour nearby + friend-network (which
#     work off location and the social graph from day one). Whatever they can't
#     fill drops through FALLBACK_ORDER to trending — exactly the "new users
#     need more nearby/trending until interests are established" behaviour B5
#     calls for.
_DISCOVERY_ORDER_MATURE = [
    "activity", "collaborative", "friend_network", "nearby", "activity", "collaborative",
]

_DISCOVERY_ORDER_COLD = [
    "nearby", "friend_network", "nearby", "friend_network", "activity", "collaborative",
]


# When a slot can't be filled by its primary rail, walk this order and pull
# the next available candidate. The slot's own rail is removed from its
# fallback list (no point re-trying ourselves).
FALLBACK_ORDER = ["activity", "collaborative", "friend_network", "nearby", "followed", "trending"]


# Quality floors per rail. Posts that don't clear these are not slotted.
# Engagement-rate (CTR) blending — C5. Smoothing is a pseudo-impression prior
# that keeps low-impression posts from spiking on a high rate; weight/scale
# tune how strongly the rate bonus moves ranking on top of raw popularity.
CTR_SMOOTHING  = 50.0

CTR_WEIGHT     = 1.5

CTR_RATE_SCALE = 10.0


FRIEND_NETWORK_MIN_SCORE  = 8.0

FRIEND_NETWORK_MIN_MUTUAL = 3

# Per-close-friend bonus to the social score (B8). A mutual follower is worth
# 3; a CLOSE friend who follows the author is worth this much more on top, so
# "3 of my close friends follow them" dominates "3 random mutuals follow them".
FRIEND_NETWORK_CLOSE_WEIGHT = 8

NEARBY_MIN_ENGAGEMENT     = 2.0    # log10(1 + likes + 2c + 3s) >= 2 ≈ 100 engagement points

NEARBY_RADIUS_KM          = 25.0

ACTIVITY_MIN_CONTENT      = 4.0

ACTIVITY_MIN_SCORE        = 6.0

ACTIVITY_COLD_START_EVENTS = 20

# Soft cold-start ramp (B7): personalization strength = n_events / 20, so
# half-strength at 10 events and full at 20. Below this floor there's too
# little signal to personalize at all and the rail bails. The "strength"
# scales the quality thresholds: a newer viewer's weaker matches are let
# through proportionally, so day-one users get *some* signal-shaped content
# instead of a hard zero until event #20.
ACTIVITY_COLD_START_MIN_SCALE = 0.25   # n_events < 5 → rail stays empty

# Time-of-day boost weight (B6): how much a match with the viewer's interests
# in the CURRENT 6-hour slice of day adds on top of their all-day affinity.
# A nudge, not a takeover.
ACTIVITY_TOD_BOOST        = 0.5

# Diversity cap: at most this many posts from any single author in the
# personal-interest rail, so a prolific favorite can't fill it (B4). Mirrors
# COLLABORATIVE_MAX_PER_AUTHOR on the collaborative rail.
ACTIVITY_MAX_PER_AUTHOR   = 2


# Recency decay half-lives, expressed in days for readability.
FRIEND_NETWORK_HALF_LIFE_DAYS = 3

NEARBY_HALF_LIFE_DAYS         = 2

ACTIVITY_HALF_LIFE_DAYS       = 5

TRENDING_HALF_LIFE_DAYS       = 1


# Candidate windows.
FRIEND_NETWORK_WINDOW_DAYS = 14

NEARBY_WINDOW_DAYS         = 7

ACTIVITY_WINDOW_DAYS       = 30

TRENDING_WINDOW_DAYS       = 3

LOCATION_FRESHNESS_DAYS    = 30   # viewer's UserProfile.location_updated_at


# Cache TTLs (seconds).
FRIEND_NETWORK_TTL_S    = 300

NEARBY_TTL_S            = 600

ACTIVITY_PROFILE_TTL_S  = 1800

# Counter-based invalidation: even within the 30-min TTL, rebuild the
# activity profile once the viewer has logged at least this many new
# Activity rows since the previous build. The spec
# (docs/FEED_RANKING_SPEC.md:247) calls this out as the right invalidation
# rule for engaged users — a TTL alone makes power users wait too long
# for their feed to adapt. Counter is bumped by utils.log_activity.
ACTIVITY_PROFILE_EVENTS_THRESHOLD = 20

ACTIVITY_SCORES_TTL_S   = 600

TRENDING_TTL_S          = 300


# Trending HASHTAGS (D2) — distinct from the post-level "trending" rail above.
# A rolling count of how many posts used each hashtag in the last
# TRENDING_HASHTAG_WINDOW_MINUTES, refreshed by the build_trending_hashtags
# management command into a single cache key. The activity rail multiplies a
# viewer's existing affinity for a hashtag by a boost when that hashtag is
# trending *right now* — so someone who follows #election topics gets that
# content surfaced harder during election week, and quietly de-emphasised the
# rest of the year, without changing their long-run profile.
TRENDING_HASHTAG_KEY            = "feed:trending_hashtags"

TRENDING_HASHTAG_WINDOW_MINUTES = 15

# TTL comfortably outlives the ~5-min rebuild cadence, but is short enough
# that if the job stops the boost simply decays away (the map expires → every
# multiplier returns to 1.0, i.e. no boost — the safe default).
TRENDING_HASHTAG_TTL_S          = 1200

# Ignore tiny noise: a tag needs at least this many posts in the window to
# count as "trending" at all.
TRENDING_HASHTAG_MIN_POSTS      = 3

# Cap the stored set so the cached map stays small.
TRENDING_HASHTAG_MAX            = 200

# How strongly a live-trending tag amplifies a viewer's affinity for it. The
# multiplier is 1 + BOOST * intensity, where intensity ∈ (0, 1] is the tag's
# share of the trending distribution — so the very hottest tag roughly doubles
# its affinity contribution (BOOST=1.0) and merely-warm tags get a fraction.
TRENDING_HASHTAG_BOOST          = 1.0


# Collaborative ("people like you") rail — B3. Reads precomputed
# RecommendedAuthor rows (built nightly), so it's cheap at request time.
COLLABORATIVE_TTL_S          = 600

COLLABORATIVE_WINDOW_DAYS    = 14   # only surface recent posts from rec'd authors

COLLABORATIVE_HALF_LIFE_DAYS = 3

COLLABORATIVE_TOP_AUTHORS    = 50   # how many recommended authors to draw posts from

COLLABORATIVE_MAX_PER_AUTHOR = 2    # diversity: cap posts per author in the pool


# Impression buffer (C1). Feed renders push their impressions onto a Redis
# list instead of writing them on the request path; the drain_impressions
# command flushes the list into the DB. IMPRESSION_QUEUE_MAX is a backlog cap:
# if the buffer reaches it (drain down or not scheduled), renders fall back to
# a synchronous write rather than piling on — so impressions are never
# silently dropped, just recorded on-path until the drain catches up.
IMPRESSION_QUEUE_KEY = "feed:impressions:queue"

IMPRESSION_QUEUE_MAX = 50000


# Session-scoped seen-post dedup window.
SESSION_DEDUP_TTL_S     = 14400   # 4 hours

SESSION_DEDUP_MAX_SIZE  = 2000    # cap to keep cache values bounded


# Hard bounds on cursor-supplied offsets to prevent pathological scans.
MAX_OFFSET = 500


# Candidate pool sizes (post-DB, pre-scoring caps).
FRIEND_NETWORK_POOL = 500

NEARBY_POOL         = 300

ACTIVITY_POOL       = 500

COLLABORATIVE_POOL  = 300

TRENDING_POOL       = 100
