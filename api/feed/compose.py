"""Final feed assembly: cross-rail dedup, slotting, author spacing, reasons, and the public compose_home_feed_page entry point."""
from __future__ import annotations
import logging

from collections import Counter, defaultdict
from urllib.parse import urlencode

from django.utils.dateparse import parse_datetime

from ..models import PostHashtag
from .affinity import _build_activity_profile
from .constants import FALLBACK_ORDER, MAX_OFFSET, PAGE_SIZE, SLOT_LAYOUT
from .cursors import _bounded_int, decode_cursor, encode_cursor
from .impressions import record_impressions
from .layout import compute_slot_layout
from .rails import _rail_activity, _rail_collaborative, _rail_friend_network, _rail_nearby, _rail_trending
from .scoring import _percentile_ranks
from .seen import get_seen_post_ids, mark_posts_seen

logger = logging.getLogger(__name__)

def dedup_across_rails(rail_lists: dict[str, list]) -> dict[str, list]:
    """
    Given {rail_name: [(post_id, score, post_obj), ...]}, remove posts that
    appear in multiple rails, keeping the post in whichever rail ranks it
    highest by percentile (C2 — distribution-free, see _percentile_ranks).

    Returns the same shape, with overlapping ids removed from the losing
    rails. Preserves per-rail ordering.
    """
    # Percentile rank of each item within its own rail.
    rail_ranks: dict[str, dict[int, float]] = {}
    for rail, items in rail_lists.items():
        rail_ranks[rail] = _percentile_ranks([s for _, s, _ in items])

    # Map post_id → list of (rail, idx, percentile_rank).
    post_rails: dict[int, list[tuple[str, int, float]]] = defaultdict(list)
    for rail, items in rail_lists.items():
        for idx, (pid, _, _) in enumerate(items):
            post_rails[pid].append((rail, idx, rail_ranks[rail].get(idx, 0.0)))

    # For posts in multiple rails, keep the one ranked highest in its rail;
    # mark the losing entries for removal.
    drop: dict[str, set[int]] = defaultdict(set)
    for pid, entries in post_rails.items():
        if len(entries) <= 1:
            continue
        entries.sort(key=lambda e: e[2], reverse=True)
        # Keep entries[0]; drop the rest.
        for rail, _, _ in entries[1:]:
            drop[rail].add(pid)

    out: dict[str, list] = {}
    for rail, items in rail_lists.items():
        out[rail] = [it for it in items if it[0] not in drop[rail]]
    return out



# =============================================================================
# Slot composition
# =============================================================================

def slot_page(rail_lists: dict[str, list], trending: list, layout=None) -> list:
    """
    Walk `layout` (defaults to SLOT_LAYOUT) and place the next-best post from
    each slot's primary rail. If that rail is empty, walk FALLBACK_ORDER
    (minus the slot's own rail) until a candidate is found. `trending` is the
    final fallback.

    `layout` is supplied per-viewer by compose_home_feed_page so a viewer's
    discovery appetite can shift the followed↔activity balance (B1 / Signal 1).

    Returns a list of (post_obj, rail_name) tuples in slot order.

    Doesn't pop from input lists destructively — uses per-rail cursors so
    every slot sees the next-best of whichever rail it ends up pulling from.
    """
    if layout is None:
        layout = SLOT_LAYOUT
    rail_cursors = {rail: 0 for rail in rail_lists}
    trending_cursor = 0
    placed: list = []

    for slot_idx, primary_rail in enumerate(layout):
        # Build the priority chain for this slot: primary first, then
        # FALLBACK_ORDER minus the primary (to avoid retrying the same rail).
        chain = [primary_rail] + [r for r in FALLBACK_ORDER if r != primary_rail]
        picked = None
        for rail in chain:
            if rail == "trending":
                if trending_cursor < len(trending):
                    pid, score, post_obj = trending[trending_cursor]
                    trending_cursor += 1
                    picked = (post_obj, "trending")
                    break
                continue
            items = rail_lists.get(rail) or []
            cur = rail_cursors[rail]
            if cur < len(items):
                pid, score, post_obj = items[cur]
                rail_cursors[rail] = cur + 1
                picked = (post_obj, rail)
                break
        if picked is None:
            # Truly nothing left to slot. Stop early — the response will
            # be shorter than PAGE_SIZE, and the cursor logic will signal
            # end-of-feed.
            break
        placed.append(picked)

    return placed



def _placed_author_id(item):
    """Author id of a placed (post_obj_or_dict, rail) item, or None."""
    post = item[0]
    if isinstance(post, dict):
        return (post.get("user") or {}).get("id")
    return getattr(post, "user_id", None)



def space_out_authors(placed):
    """
    De-cluster same-author posts (B10). Two rails can independently surface
    the same author, leaving their posts back-to-back in the page. Walk the
    placed list and, wherever an author repeats adjacently, try swapping the
    second occurrence with another slot — preferring later slots, then
    earlier ones — accepting the first swap that leaves NEITHER swapped
    position adjacent to a same-author neighbour.

    The swap is tentatively applied and verified against the actual resulting
    arrangement (rather than reasoned about in advance), which keeps the
    logic correct even when the swap partner is the immediate next slot. If
    no swap helps (e.g. one author genuinely dominates the page), the item is
    left in place — best effort, never worse.

    Mutates and returns `placed`. Rail labels travel with their posts and the
    rail multiset is unchanged, so downstream `consumed` counts and the
    cursor are unaffected.
    """
    n = len(placed)

    def aid(k):
        return _placed_author_id(placed[k])

    def clean_at(k):
        """True if slot k has no same-author neighbour."""
        a = aid(k)
        if a is None:
            return True
        if k > 0 and aid(k - 1) == a:
            return False
        if k < n - 1 and aid(k + 1) == a:
            return False
        return True

    # Single left-to-right sweep. At each collision, try a FORWARD swap first:
    # the colliding post moves to a later slot we haven't visited yet, so if
    # that move creates a new collision there, the same sweep will fix it when
    # it reaches that slot (only clean_at(i) needs to hold now). If no forward
    # slot works (e.g. the collision is at the end of the page), fall back to a
    # BACKWARD swap, which must clean both ends since the destination has
    # already been visited. Already-cleaned slots to the left are never
    # disturbed, so the sweep terminates and can't oscillate.
    for i in range(1, n):
        if aid(i) is None or aid(i) != aid(i - 1):
            continue
        fixed = False
        for j in range(i + 1, n):                 # forward
            placed[i], placed[j] = placed[j], placed[i]
            if clean_at(i):
                fixed = True
                break
            placed[i], placed[j] = placed[j], placed[i]   # revert
        if fixed:
            continue
        for j in range(i - 2, -1, -1):            # backward fallback
            placed[i], placed[j] = placed[j], placed[i]
            if clean_at(i) and clean_at(j):
                break
            placed[i], placed[j] = placed[j], placed[i]   # revert
    return placed



# =============================================================================
# "Why am I seeing this?" reasons (D3)
# =============================================================================
#
# Each served post already carries its source `rail`. We turn that into a
# structured reason the client shows behind a small "i": human-readable text
# PLUS the specific target it cites (a hashtag / author / page). The target is
# what closes the loop — when a viewer taps "show less" on a post explained by
# "#cooking", the client can fire a not-interested for exactly that topic, so
# the dismissal teaches something specific (B2).

def _reason_for(d: dict, hashtag_aff: dict, post_tags: dict) -> dict:
    rail = d.get("rail")
    u = d.get("user") or {}
    page = d.get("page")

    def reason(type_, text, **extra):
        base = {
            "rail": rail, "type": type_, "text": text,
            "hashtag": None, "author_id": None, "page_id": None,
        }
        base.update(extra)
        return base

    if rail == "followed":
        if page and page.get("name"):
            return reason("page", f"From {page['name']}, which you follow",
                          page_id=page.get("id"))
        uname = u.get("username") or "someone you follow"
        return reason("follow", f"Because you follow {uname}", author_id=u.get("id"))

    if rail == "friend_network":
        return reason("friends", "Popular with people you follow", author_id=u.get("id"))

    if rail == "nearby":
        return reason("nearby", "Posted near you")

    if rail == "collaborative":
        return reason("similar", "Popular with people who like what you like",
                      author_id=u.get("id"))

    if rail == "activity":
        # Cite the post's hashtag the viewer is most into (their highest
        # affinity among this post's tags). Falls back to a generic
        # activity reason when the match was by author rather than topic.
        best, best_aff = None, 0.0
        for t in post_tags.get(d.get("id"), ()):
            a = hashtag_aff.get(t, 0.0)
            if a > best_aff:
                best, best_aff = t, a
        if best:
            return reason("interest_hashtag",
                          f"Based on your interest in #{best}", hashtag=best)
        return reason("interest", "Based on your recent activity", author_id=u.get("id"))

    if rail == "trending":
        return reason("trending", "Trending now")

    return reason("suggested", "Suggested for you")



def _attach_reasons(serialized: list, profile: dict) -> None:
    """Attach a `reason` to every served post (D3). One batched PostHashtag
    query covers the activity-rail posts that may warrant a specific
    hashtag reason; everything else is derived from the post dict alone."""
    hashtag_aff = (profile or {}).get("hashtag") or {}
    activity_ids = [d["id"] for d in serialized if d.get("rail") == "activity"]
    post_tags: dict[int, list] = defaultdict(list)
    if activity_ids and hashtag_aff:
        try:
            for pid, tag in (
                PostHashtag.objects
                .filter(post_id__in=activity_ids)
                .values_list("post_id", "hashtag")
            ):
                post_tags[pid].append(tag)
        except Exception:
            pass
    for d in serialized:
        try:
            d["reason"] = _reason_for(d, hashtag_aff, post_tags)
        except Exception:
            d["reason"] = {
                "rail": d.get("rail"), "type": "suggested",
                "text": "Suggested for you",
                "hashtag": None, "author_id": None, "page_id": None,
            }



# =============================================================================
# Public entry point
# =============================================================================

def compose_home_feed_page(request, user, serialize_post_fn, build_feed_context_fn):
    """
    Build one page of the home feed for `user`.

    `serialize_post_fn` and `build_feed_context_fn` are passed in from
    views.py to avoid a circular import (views.py already imports from
    this module; serialize_post / build_feed_context live there).

    Returns a dict ready to wrap in a DRF Response:
        {"results": [...], "next": <absolute_url|None>}
    """
    context = build_feed_context_fn(user)

    cursor = decode_cursor(request.query_params.get("cursor"))
    f_cursor    = cursor.get("f_cursor")
    f_cursor_id = cursor.get("f_cursor_id")
    s_offset    = _bounded_int(cursor.get("s_offset", 0))
    n_offset    = _bounded_int(cursor.get("n_offset", 0))
    a_offset    = _bounded_int(cursor.get("a_offset", 0))
    c_offset    = _bounded_int(cursor.get("c_offset", 0))

    before = parse_datetime(f_cursor) if isinstance(f_cursor, str) else None
    before_id = None
    if f_cursor_id is not None:
        try:
            before_id = int(f_cursor_id)
        except (TypeError, ValueError):
            before_id = None

    seen_ids = get_seen_post_ids(user.id)

    # ------------------------------------------------------------------
    # Per-viewer slot layout (B5). compute_slot_layout sizes the followed↔
    # discovery balance from the viewer's follow count, taste-profile maturity
    # (n_events), and discovery appetite (B1's tab-usage signal, folded in).
    # The activity profile is cached, so reading it here is cheap. The per-rail
    # slot counts drive both the page layout and how many candidates we fetch
    # from each rail.
    # ------------------------------------------------------------------
    layout = SLOT_LAYOUT
    _profile = {}   # also reused below to build the "why am I seeing this" reasons (D3)
    try:
        _profile = _build_activity_profile(user)
        layout = compute_slot_layout(
            follow_count=len(context["followed_users"]),
            n_events=_profile.get("n_events", 0),
            discovery_appetite=_profile.get("discovery_appetite"),
        )
    except Exception as exc:   # never let layout selection break the feed
        logger.error(f"[compose_home_feed_page] layout selection failed: {exc}")

    # Per-rail slot counts for THIS layout. Used to size each rail's fetch and
    # the fallback-needed check below. A rail may get 0 slots for some viewers
    # (e.g. activity/collaborative for a cold-start user); we still fetch a
    # small amount from every discovery rail (the max(...,1) below) so it can
    # serve as a FALLBACK for other rails' empty slots.
    slots = Counter(layout)

    # ------------------------------------------------------------------
    # Pull each rail in parallel-by-function-call (each rail is cached;
    # the actual DB cost only fires on cache misses).
    # ------------------------------------------------------------------

    # Followed rail is supplied by views.get_followed_feed — call it via
    # serialize_post_fn's module so we don't import views from here.
    # We import it lazily to break the cycle.
    from ..views import get_followed_feed
    followed_raw = get_followed_feed(
        request=request, user=user, context=context,
        before=before, before_id=before_id,
    )
    # get_followed_feed returns serialized dicts — convert to the
    # (post_id, score, post_obj_or_dict) shape rails use. We use the dict
    # as the third tuple slot since it's already serialized.
    followed_items = []
    for p in followed_raw:
        pid = p.get("id")
        if pid is None or pid in seen_ids:
            continue
        followed_items.append((pid, 0.0, p))  # score unused for followed

    # Limit followed list to ~2x the slots it might fill to keep dedup
    # cheap. Excess is harmless.
    followed_items = followed_items[: slots["followed"] * 2]

    # Each discovery rail fetches at least a little (max(..,1)) even when this
    # viewer's layout allocates it 0 slots, so it remains available as a
    # FALLBACK for other rails' unfilled slots.
    friend_items   = _rail_friend_network(
        request, user, context,
        offset=s_offset, limit=max(slots["friend_network"], 1) * 2,
        exclude_ids=seen_ids,
    )
    nearby_items   = _rail_nearby(
        request, user, context,
        offset=n_offset, limit=max(slots["nearby"], 1) * 2,
        exclude_ids=seen_ids,
    )
    activity_items = _rail_activity(
        request, user, context,
        offset=a_offset, limit=max(slots["activity"], 1) * 2,
        exclude_ids=seen_ids,
    )
    collaborative_items = _rail_collaborative(
        request, user, context,
        offset=c_offset, limit=max(slots["collaborative"], 1) * 2,
        exclude_ids=seen_ids,
    )

    # Cross-rail dedup. Followed is not deduplicated against the others —
    # if a followed post would also have shown up via friend-network, the
    # user is going to see it once either way; we prefer the followed
    # source for label clarity. So we only dedup the discovery rails.
    discovery_dedup = dedup_across_rails({
        "friend_network": friend_items,
        "nearby":         nearby_items,
        "activity":       activity_items,
        "collaborative":  collaborative_items,
    })

    # Followed-vs-discovery dedup: if the same post id appears in both,
    # drop it from discovery (the followed rail wins).
    followed_ids = {it[0] for it in followed_items}
    for rail in ("friend_network", "nearby", "activity", "collaborative"):
        discovery_dedup[rail] = [it for it in discovery_dedup[rail] if it[0] not in followed_ids]

    rail_lists = {
        "followed":       followed_items,
        "friend_network": discovery_dedup["friend_network"],
        "nearby":         discovery_dedup["nearby"],
        "activity":       discovery_dedup["activity"],
        "collaborative":  discovery_dedup["collaborative"],
    }

    # Pull trending only if we expect to need it (any rail short of the
    # slots THIS layout asks of it).
    rails_needing_fallback = any(
        len(rail_lists[r]) < slots.get(r, 0) for r in rail_lists
    )
    trending_items: list = []
    if rails_needing_fallback:
        trending_items = _rail_trending(
            request, user, context,
            limit=PAGE_SIZE,
            exclude_ids=seen_ids | {it[0] for it in followed_items}
                                  | {it[0] for it in rail_lists["friend_network"]}
                                  | {it[0] for it in rail_lists["nearby"]}
                                  | {it[0] for it in rail_lists["activity"]}
                                  | {it[0] for it in rail_lists["collaborative"]},
        )

    # Slot the page using this viewer's layout.
    placed = slot_page(rail_lists, trending_items, layout=layout)

    # B10: nudge apart any two posts by the same author that ended up adjacent
    # (different rails can surface the same author independently). Pure
    # reorder — the rail multiset is unchanged, so `consumed` below is intact.
    placed = space_out_authors(placed)

    # Count how many of each rail we actually consumed so we can advance offsets.
    consumed = defaultdict(int)
    for _, rail in placed:
        consumed[rail] += 1

    # ------------------------------------------------------------------
    # Serialize the placed posts. Followed posts are already dicts; the
    # other rails carry annotated Post objects that need serialize_post.
    # ------------------------------------------------------------------
    serialized: list = []
    for post_or_dict, rail in placed:
        if isinstance(post_or_dict, dict):
            d = dict(post_or_dict)
            d["rail"] = rail
            # serialize_post sets `suggested=False` for followed, but the
            # rail label is the source of truth going forward.
            d.setdefault("suggested", rail != "followed")
            serialized.append(d)
        else:
            d = serialize_post_fn(
                post=post_or_dict, user=user, request=request,
                suggested=True, top_comments=[],
            )
            d["rail"] = rail
            serialized.append(d)

    # ------------------------------------------------------------------
    # Attach a "why am I seeing this?" reason to each post (D3), using the
    # rail + the viewer's affinity profile we already loaded above.
    # ------------------------------------------------------------------
    _attach_reasons(serialized, _profile)

    # ------------------------------------------------------------------
    # Mark the served posts as seen so they don't reappear this session.
    # ------------------------------------------------------------------
    mark_posts_seen(user.id, [d["id"] for d in serialized])

    # ------------------------------------------------------------------
    # Record impressions for this render (C1). These are post_impression
    # Activity rows (the engagement-rate denominator from A2/C5) plus the
    # Post.impression_count bump. record_impressions pushes them to a Redis
    # buffer so the writes happen OFF the request path (drained by the
    # `drain_impressions` command), and falls back to a synchronous write
    # when Redis is unavailable or the buffer is backed up — so nothing is
    # lost. This replaces the old per-request daemon thread that opened a DB
    # connection per request and exhausted the pool under load.
    # ------------------------------------------------------------------
    record_impressions(user, serialized)

    # ------------------------------------------------------------------
    # Build the next cursor.
    # ------------------------------------------------------------------
    next_url = None
    if len(serialized) == PAGE_SIZE:
        followed_in_page = [d for d in serialized if d.get("rail") == "followed"]
        # B9: a fresh-follow post can carry an OLD created_at (a new follow's
        # back-catalogue). It's boosted to the top of the followed rail, but it
        # must NOT set the chronological cursor — otherwise the next page would
        # resume from that old timestamp and skip everything in between. Base
        # the cursor on the genuinely chronological followed posts, falling
        # back to all followed posts only if the page was entirely fresh-follow.
        cursor_pool = [d for d in followed_in_page if not d.get("is_fresh_follow")]
        if not cursor_pool:
            cursor_pool = followed_in_page
        next_f_cursor = None
        next_f_cursor_id = None
        if cursor_pool:
            oldest = min(
                cursor_pool,
                key=lambda d: (d["created_at"], d["id"]),
            )
            ca = oldest["created_at"]
            next_f_cursor = ca.isoformat() if hasattr(ca, "isoformat") else str(ca)
            next_f_cursor_id = oldest["id"]
        elif f_cursor is not None:
            next_f_cursor = f_cursor
            next_f_cursor_id = f_cursor_id

        next_cursor_payload = {
            "f_cursor":    next_f_cursor,
            "f_cursor_id": next_f_cursor_id,
            "s_offset":    min(s_offset + consumed["friend_network"], MAX_OFFSET),
            "n_offset":    min(n_offset + consumed["nearby"], MAX_OFFSET),
            "a_offset":    min(a_offset + consumed["activity"], MAX_OFFSET),
            "c_offset":    min(c_offset + consumed["collaborative"], MAX_OFFSET),
        }
        token = encode_cursor(next_cursor_payload)
        next_url = request.build_absolute_uri(
            f"{request.path}?{urlencode({'cursor': token})}"
        )

    # Fold `following_count` into the response directly using the context
    # we already built at the top of this function. Previously home_feed
    # had to call build_feed_context a second time just to read this
    # single integer; both calls hit the same 90 s cache, but the extra
    # cache.get is gone now.
    return {
        "results":         serialized,
        "next":            next_url,
        "following_count": len(context["followed_users"]),
    }
