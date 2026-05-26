"""
Home feed ranking pipeline.

Implements the four-rail composition defined in docs/FEED_RANKING_SPEC.md:

    (a) followed           — 6 slots/page (recency, existing logic)
    (b) friend-network     — 1 slot/page  (mutual graph overlap, dominant)
    (c) nearby             — 1 slot/page  (haversine within 25 km, dominant)
    (d) activity-based     — 2 slots/page (per-viewer author/hashtag affinity)

Plus a global-trending fallback used only when every rail's fallback chain
is exhausted.

The public entry point is `compose_home_feed_page(request, user)`. It returns
a dict shaped `{"results": [...], "next": <url|None>}` ready to be wrapped in
a DRF Response.

Conventions inside this module:

* Each rail's `_rail_<name>(...)` function returns
  `[(post_id: int, score: float, post_obj: Post), ...]` already sorted
  best-first. Post objects are annotated for `serialize_post`.
* Cross-rail deduplication runs *after* every rail has produced its list,
  keeping the post under the rail with the highest z-score.
* `slot_page(...)` walks the position layout, pulling from each rail's
  candidate list with a documented fallback chain when a rail is empty
  or has no candidate clearing its quality floor.
* The composite cursor is a base64-encoded JSON dict — opaque to the
  client, validated server-side on every call.

Caching: each rail caches its scored candidate id list (NOT the serialized
posts), so pagination only re-fetches the slice we need. Cache keys include
either the viewer id alone or viewer id + a coarse geohash / activity-version
so the cache invalidates when the input space materially changes.
"""

from .compose import compose_home_feed_page

__all__ = ["compose_home_feed_page"]
