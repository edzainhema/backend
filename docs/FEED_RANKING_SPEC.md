# Home Feed Ranking Spec

Per-page composition for the authenticated home feed.

## Page composition

Each page returned by `/feed/` has 10 slots, composed of four rails:

| Rail | Slots / page | Source |
|---|---|---|
| (a) Followed | 6 | Posts from users / pages the viewer follows |
| (b) Friend-network | 1 | Posts from non-followed authors with strong mutual-graph overlap |
| (c) Nearby | 1 | Trending posts within ~25 km of the viewer's last fix |
| (d) Activity-based | 2 | Posts ranked by the viewer's own engagement history |

Total: 6 followed (60%) + 4 discovery (40%).

### Slotting order within a page

Discovery posts are scattered through the page rather than blocked at the end. Target positions (1-indexed) within each page of 10:

```
1  2  3  4  5  6  7  8  9  10
F  F  F  A  F  F  N  A  S  F
```

Where F = followed, S = friend-network (b), N = nearby (c), A = activity (d). Final order is not load-bearing; rule of thumb: never two discovery slots adjacent, never a discovery slot first.

If a rail can't fill its slot (quality floor not met, candidates exhausted), the slot **falls through** to the next-priority rail in this order: `activity → friend-network → nearby → followed → global trending`. Never leave a slot empty.

---

## Rail (a) — Followed

**Already implemented in `get_followed_feed`.** Document current behavior, no changes proposed yet.

| Aspect | Value |
|---|---|
| Candidate set | Posts from `followed_users` ∪ posts in `followed_pages`, respecting privacy rules in `get_followed_feed` |
| Primary signal | Recency (`-created_at`) |
| Quality floor | None |
| Fallback | If <6 followed posts available, fall through to activity → friend-network → nearby |
| TTL | None (live query, cursor-based) |

**Open question:** Should followed posts be ranked beyond pure recency? E.g. demote posts the viewer has already seen/skipped? Defer to v2.

---

## Rail (b) — Friend-network

**Already implemented in `get_suggested_feed`; needs a separate scoring path now that activity is its own rail.**

### Candidate set

```
Post.objects
  .exclude(user_id__in=followed_users)
  .exclude(user_id=viewer.id)
  .exclude(user_id__in=blocked_user_ids ∪ muted_user_ids)
  .exclude(page_id__in=muted_page_ids)
  .exclude(super_private_page_not_followed)
  .filter(created_at__gte=now - 14d)        # recency window
  .order_by("-created_at")[:500]
```

### Score

```
mutual_followers     = |viewer_followers ∩ author_followers|
mutual_following     = |viewer_following ∩ author_following|

social_score    = mutual_followers * 3 + mutual_following * 2
engagement_lift = log10(1 + likes + 2*comments + 3*saves)
score = (social_score + 2 * engagement_lift) * recency_decay(post.created_at)
```

Note: engagement is a *tiebreaker* here, not the dominant signal. Dominant signal is social-graph overlap. Otherwise (b) collapses into (d).

### Quality floor

`score >= 8` AND `mutual_followers + mutual_following >= 3`.

If no candidate clears the floor, the slot falls through.

### Fallback

`activity → nearby → global trending`.

### Cache

Key: `feed:friend_network:{viewer.id}`. TTL 5 min. Invalidate on follow/unfollow.

---

## Rail (c) — Nearby

**New.** Uses `UserProfile.latitude/longitude` (viewer) and `Post.upload_latitude/upload_longitude`.

### Candidate set

```
Post.objects
  .exclude(user_id__in=followed_users ∪ blocked ∪ muted)
  .exclude(page_id__in=muted_page_ids ∪ super_private_unfollowed)
  .exclude(upload_latitude__isnull=True)
  .filter(created_at__gte=now - 7d)              # tighter window: local should feel current
  .filter(haversine(viewer.lat, viewer.lng, upload_latitude, upload_longitude) <= 25)
  [:300]
```

Haversine is computed in the candidate query via a `Func` annotation (Postgres `earth_distance` or hand-rolled `acos(sin*sin + cos*cos*cos)` if PostGIS isn't installed). Bounding-box prefilter on lat/lng first to limit rows.

### Score

```
distance_km     = haversine(viewer, post)
proximity       = max(0, 1 - distance_km / 25)            # 0..1, linear within radius
engagement      = log10(1 + likes + 2*comments + 3*saves)
activity_match  = 1 + 0.5 * has_engaged_with_hashtag(viewer, post)
                    + 0.3 * has_visited_author(viewer, post)
score = (proximity * 3 + engagement * 2) * activity_match * recency_decay(post.created_at, half_life=2d)
```

`activity_match` is the small personalization term — keeps location dominant but ensures a viewer who's never engaged with food content doesn't get nearby food posts when there are nearby music posts available.

### Quality floor

`engagement >= 2` (≈ at least one of: 10 likes, or 5 comments, or 3 saves) AND `distance_km <= 25`.

If viewer has no location (`UserProfile.latitude is None` or `location_updated_at` older than 30 days): rail is **disabled**, slot falls through to activity.

### Fallback

`activity → friend-network → global trending`.

### Cache

Key: `feed:nearby:{viewer.id}:{geohash5}` (geohash precision 5 ≈ 5 km cells, so the cache survives small movements but invalidates when the viewer travels). TTL 10 min.

---

## Rail (d) — Activity-based

**New.** This is the only fully personalized rail. Two slots per page, so the candidate pool needs to be strongest here.

### Available signals (already logged)

From `Activity` and the specialized tables:

| Signal | Strength | Source |
|---|---|---|
| `post_like` | Strong explicit positive | `PostLike` |
| `post_save` | Strongest explicit positive | `SavedPost` |
| `post_comment` | Strong explicit positive | `Comment` |
| `post_share` | Strong explicit positive | `Activity[post_share]` |
| `reel_complete` / `watched_to_end` | Strong implicit positive | `Activity[reel_complete]`, `ReelWatch.seconds_watched` |
| `reel_rewatch` | Strongest implicit positive | `Activity[reel_rewatch]` |
| `post_view` with long `duration_seconds` | Implicit positive | `Activity[post_view]` |
| `post_dwell` | Weak implicit positive | `Activity[post_dwell]` |
| `user_visit`, `page_visit` | Author affinity | `Activity[user_visit/page_visit]`, `ProfileVisit` |
| `hashtag_engage` | Topic affinity | `Activity[hashtag_engage]` |
| `search_query`, `search_click` | Intent | `Activity[search_*]` |
| `reel_skip` | Strong negative | `Activity[reel_skip]` |
| `post_unlike`, `post_unsave` | Negative | `Activity[post_unlike/unsave]` |
| `Activity.keywords` | Topic / intent tags | extracted on comments |

### Scoring approach: author-affinity v1

Build a per-viewer profile lazily (cached), then score candidate posts against it.

#### Viewer profile (cached 30 min)

```
author_affinity:  {author_id -> score}
  +5  per post_save
  +3  per post_like
  +3  per post_comment (positive/neutral sentiment)
  +1  per post_comment (negative)
  +4  per reel_rewatch
  +3  per reel_complete
  +2  per post_view with duration >= 8s
  +2  per user_visit
  +1  per page_visit (apply to all authors who posted in that page recently)
  -3  per reel_skip on that author
  -3  per post_unlike

  Multiply each event's contribution by exp(-age_days / 14)
  (so a like 14 days ago counts ~37% of a like today.)
  Keep top 200 authors. Discard the rest.

hashtag_affinity:  {hashtag -> score}
  +3  per hashtag_engage
  +2  per search_query containing the hashtag
  +1  per post_save where post.description contains #hashtag
  Time-decayed identically.
  Keep top 50 hashtags.

keyword_affinity: {keyword -> score}
  From Activity.keywords on the viewer's own engagements.
  Keep top 50.
```

#### Candidate set

```
Post.objects
  .exclude(user_id__in=followed_users ∪ blocked ∪ muted)
  .exclude(page_id__in=muted_page_ids ∪ super_private_unfollowed)
  .filter(created_at__gte=now - 30d)
  .filter(
      Q(user_id__in=top_50_authors)
      | Q(description__regex=top_20_hashtags_pattern)
      | Q(id__in=posts_with_matching_keywords)
  )
  [:500]
```

Plus: include posts from authors *visited* in the last 7 days even if not in the affinity top 200, to surface "I just looked at this profile, show me more from them."

#### Score

```
author_score    = author_affinity.get(post.user_id, 0)
hashtag_score   = sum(hashtag_affinity[h] for h in extract_hashtags(post.description))
keyword_score   = sum(keyword_affinity[k] for k in post.keywords if Activity-tagged)

content_match   = author_score + 0.5 * hashtag_score + 0.3 * keyword_score
engagement      = log10(1 + likes + 2*comments + 3*saves)

score = (content_match * 2 + engagement) * recency_decay(post.created_at, half_life=5d)
```

#### Quality floor

`content_match >= 4` AND `score >= 6`. Otherwise slot falls through.

#### Cold start

Viewer with < 20 total `Activity` events: rail is **disabled**, both slots fall through to `friend-network → nearby → global trending`. Re-evaluate every page until the threshold is crossed.

#### Fallback

`friend-network → nearby → global trending`.

#### Cache

- Viewer profile: `feed:activity_profile:{viewer.id}` — TTL 30 min, invalidated when viewer logs ≥20 new events since last build.
- Scored candidates: `feed:activity_scores:{viewer.id}` — TTL 10 min.

---

## Global trending (final fallback only)

When every other rail's fallback chain is exhausted. Pure engagement + recency, no personalization, no geo:

```
Post.objects
  .exclude(<all the usual exclusions>)
  .filter(created_at__gte=now - 3d)
  .annotate(score=log10(likes + 2*comments + 3*saves) * recency_decay(created_at, half_life=1d))
  .order_by("-score")[:100]
```

Cached globally (not per user) for 5 min. This is also what powers `/explore/` for unauthenticated previews.

---

## Cross-rail rules

### Deduplication

After each rail produces its top-K candidates, before slotting:

1. Collect all (post_id, rail, score) tuples.
2. For any post appearing in multiple rails, keep only the (rail, score) with the highest *normalized* score (z-score within rail, since raw scores aren't comparable across rails).
3. Drop the post from the other rails' candidate lists.

This guarantees the same post never fills two discovery slots on one page.

### Cross-page dedup (within session)

Track a session-scoped `seen_post_ids` set (Redis, keyed on session/jwt). Exclude from all rails for 4 hours. Prevents seeing the same suggested post on every page even if scores haven't changed.

### Cursor / pagination

Single composite cursor in the response: base64-encoded JSON

```json
{
  "f_cursor":   "2026-05-13T11:22:33Z",   // followed feed: timestamp of oldest post seen
  "f_cursor_id": 12345,                    // tiebreaker for created_at ties
  "s_offset":   12,                         // friend-network: position in scored list
  "n_offset":   4,                          // nearby: position
  "a_offset":   8                           // activity: position
}
```

Clients treat the cursor as opaque. Server validates and bounds each offset (max 500) to prevent abuse.

### Privacy

Every rail reuses `build_feed_context()`'s exclusion sets (`blocked_user_ids`, `muted_user_ids`, `muted_page_ids`, `super_private_pages`). **No rail may build its own exclusions** — drift between rails is how a blocked user's post ends up in front of you.

### Logging

Every served post is logged to `Activity[post_dwell]` with:
- `surface = "home"`
- `metadata = { rail: "followed" | "friend_network" | "nearby" | "activity" | "trending", score: float, slot_position: int }`

This becomes the training data for any future ML-based ranker, and the source of truth for A/B comparisons.

---

## Constants (one place to tune)

```python
PAGE_SIZE                  = 10
RAIL_SLOTS                 = {"followed": 6, "friend_network": 1, "nearby": 1, "activity": 2}
RAIL_FALLBACK_ORDER        = ["activity", "friend_network", "nearby", "followed", "trending"]

# Quality floors
FRIEND_NETWORK_MIN_SCORE   = 8
FRIEND_NETWORK_MIN_MUTUAL  = 3
NEARBY_MIN_ENGAGEMENT      = 2
NEARBY_RADIUS_KM           = 25
ACTIVITY_MIN_CONTENT_MATCH = 4
ACTIVITY_MIN_SCORE         = 6
ACTIVITY_COLD_START_EVENTS = 20

# Recency
FRIEND_NETWORK_HALF_LIFE_DAYS = 3
NEARBY_HALF_LIFE_DAYS         = 2
ACTIVITY_HALF_LIFE_DAYS       = 5

# Caches
FRIEND_NETWORK_TTL_S       = 300
NEARBY_TTL_S               = 600
ACTIVITY_PROFILE_TTL_S     = 1800
ACTIVITY_SCORES_TTL_S      = 600
TRENDING_TTL_S             = 300

# Windows
LOCATION_FRESHNESS_DAYS    = 30
SESSION_DEDUP_TTL_S        = 14400   # 4 hours
```

---

## Open decisions for the team

1. **Affinity decay half-life for activity profile (currently 14 days).** Faster decay = more reactive to recent interests. Slower = more stable identity. Worth A/B testing.
2. **Should `reel_rewatch` count toward home-feed author affinity at all?** Reels engagement and home-feed taste sometimes diverge.
3. **Min mutual count for friend-network (currently 3).** In a small user base early on, this might be too strict.
4. **Do you want a "diversity penalty" within the followed rail?** E.g. never two consecutive posts from the same author. Common UX win, small implementation.
5. **Author-affinity v1 vs. collaborative v2.** v1 (this doc) finds posts from authors the viewer has engaged with. v2 would find posts from authors that *users similar to the viewer* engaged with — bigger lift but needs a second table or precomputed similarity. Decide whether v2 is on the 6-month roadmap.
6. **Slot positions.** The 1/2/3/4/5/6/7/8/9/10 layout above is a guess. Test it. Some teams find one big discovery block more engaging; others find scattered slots feel less algorithmic.

