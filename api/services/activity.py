"""Activity-feed logging: build / bump-counter / log a user activity row."""
import logging

logger = logging.getLogger(__name__)


def build_activity(user, action_type, **kwargs):
    """
    Construct (but do NOT save) an Activity row from loosely-typed kwargs.

    This is the shared field-mapping core of log_activity, split out so the
    batch-ingest endpoint can accumulate many instances and persist them with a
    single Activity.objects.bulk_create (BACKEND_SCALING_AUDIT.md SY-3) instead
    of one INSERT per event. Pure construction — it touches neither the DB nor
    the cache — so the caller decides when, and how many rows, to write.

    Returns an unsaved Activity instance, or None when `user` isn't an
    authenticated user (the same guard log_activity has always had). Unknown
    kwargs are folded into `metadata`, and the current request's session id is
    attached when the caller didn't pass one — identical to the original logic.
    """
    # Imported lazily so utils.py stays importable before app-loading is done.
    from ..models import Activity

    if user is None or not getattr(user, "is_authenticated", False):
        return None

    valid_fields = {
        "post", "page", "target_user", "comment",
        # FK ID forms — Django accepts post_id, page_id, etc. directly
        "post_id", "page_id", "target_user_id", "comment_id",
        "duration_seconds", "surface", "channel", "tab",
        "watched_to_end", "is_rewatch", "is_skip",
        "query", "sentiment_label", "sentiment_score",
        "keywords", "hashtag", "metadata", "session_id",
    }

    direct = {k: v for k, v in kwargs.items() if k in valid_fields}
    extras = {k: v for k, v in kwargs.items() if k not in valid_fields}
    if extras:
        meta = direct.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        meta.update(extras)
        direct["metadata"] = meta

    # C3: tag the row with the current request's session id (set by
    # SessionIdMiddleware from the X-Session-Id header) unless the caller
    # passed one explicitly. This groups every event from one visit without
    # having to thread session_id through each view.
    if not direct.get("session_id"):
        try:
            from .session_context import get_current_session_id
            sid = get_current_session_id()
            if sid:
                direct["session_id"] = sid
        except Exception:
            pass

    return Activity(user=user, action_type=action_type, **direct)


def bump_activity_counter(user_id, n=1):
    """
    Increment the per-user activity counter (feed:activity_count:<id>) by `n`.

    The feed uses this counter to rebuild the cached activity profile once the
    viewer has logged 20+ new events since the last build, instead of waiting
    out the 30-minute TTL (docs/FEED_RANKING_SPEC.md:247). Power users (50+
    events in 5 min) were otherwise waiting up to 30 min for their feed to
    adapt; this closes that gap.

    `cache.incr` is atomic on Redis (the project's cache backend) but raises
    ValueError if the key doesn't exist yet — the standard idiom is try/except
    with a set() bootstrap. Expiry is generous (24h): it only needs to outlast
    a session, and _build_activity_profile snapshots the value on every rebuild.
    Best-effort — a cache outage must NEVER break the analytics write.

    Splitting this out lets the batch endpoint bump ONCE by the number of rows
    written, rather than paying a cache round trip per event (SY-3).
    """
    if n <= 0:
        return
    try:
        from django.core.cache import cache
        counter_key = f"feed:activity_count:{user_id}"
        try:
            cache.incr(counter_key, n)
        except ValueError:
            cache.set(counter_key, n, timeout=86400)
    except Exception as cache_exc:
        logger.warning(f"[log_activity] counter bump failed: {cache_exc}")


def log_activity(user, action_type, **kwargs):
    """
    Best-effort single-event activity write: build the row, INSERT it, and bump
    the per-user counter by 1.

    Never raises — analytics must never break the request path. Unknown kwargs
    are dropped into `metadata` so callers don't have to keep the schema in
    their head.

    For high-volume batched ingest, prefer build_activity + bulk_create +
    bump_activity_counter(n) so N events cost one INSERT and one counter bump
    instead of N of each — see api/views/activity_batch.py (SY-3).

    Usage:
        log_activity(request.user, "post_like", post=post, surface="home")
        log_activity(request.user, "search_query", query="sunset")
    """
    activity = build_activity(user, action_type, **kwargs)
    if activity is None:
        return None

    try:
        activity.save()
    except Exception as exc:
        # Analytics writes must never break the user request.
        logger.error(f"[log_activity] failed: {exc}")
        return None

    bump_activity_counter(user.id, 1)
    return activity


# ──────────────────────────────────────────────────────────────────────────────
# Hashtag indexing
# ──────────────────────────────────────────────────────────────────────────────

# Hard cap on hashtags persisted per post. A description can technically
# contain hundreds of "#" tokens; storing them all is pointless (a post
# spamming 200 tags is not "about" 200 topics) and would bloat the index.
# 30 is comfortably above any genuine caption.
