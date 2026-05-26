"""Session-scoped seen-post dedup (Redis ZSET in prod, locked LocMemCache fallback)."""
from __future__ import annotations
import logging

import threading
from typing import Iterable

from django.core.cache import cache
from django.utils import timezone

from .constants import SESSION_DEDUP_MAX_SIZE, SESSION_DEDUP_TTL_S

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session-scoped seen-post dedup. Keyed per user, 4-hour TTL.
#
# Two storage paths:
#
#   • Redis backend (production): a native Redis sorted-set (ZSET), updated
#     with an atomic ZADD + EXPIRE + ZREMRANGEBYRANK pipeline. ADD is a
#     merge, never an overwrite, so two concurrent home-feed requests for
#     the same user (e.g. a double pull-to-refresh on a flaky connection)
#     can't clobber each other's writes. This is the fix for
#     ACTIVITY_AND_FEED_AUDIT.md item A10. The score is a millisecond
#     timestamp, so the cap-trim (ZREMRANGEBYRANK 0, -(N+1)) drops the
#     OLDEST entries — same "keep most recent N" semantics the list path
#     had, but race-free.
#
#   • Non-Redis backend (LocMemCache dev/test, or Redis unreachable): the
#     original read-modify-write list, now guarded by a process-local lock
#     so it's at least atomic within a single process. LocMemCache isn't
#     shared across processes, so a per-process lock fully serializes the
#     RMW for that backend — the cross-process race only exists with a
#     shared store, which by definition means Redis, which uses the atomic
#     path above.
#
# The two paths use DIFFERENT cache keys on purpose. django-redis stores
# cache.set() values as a pickled string under its key; calling ZADD on
# that key would raise WRONGTYPE. The native-ZSET path therefore uses its
# own "feed:seenz:" namespace, and the legacy "feed:seen:" list keys simply
# age out under their 4-hour TTL.
# ---------------------------------------------------------------------------

# Guards the LocMemCache fallback's read-modify-write. Module-level so it's
# shared by every thread in the process (which is the only scope that
# matters for a per-process cache).
_seen_fallback_lock = threading.Lock()



def _seen_key(user_id: int) -> str:
    """Legacy list-path key (LocMemCache / fallback)."""
    return f"feed:seen:{user_id}"



def _seen_zkey(user_id: int) -> str:
    """Native-Redis ZSET key. Distinct namespace to avoid a WRONGTYPE
    collision with any pickled-list value left under _seen_key."""
    return f"feed:seenz:{user_id}"



def _seen_redis():
    """
    Return the raw redis-py client if the cache backend is django-redis,
    else None. Wrapped in try/except so a missing dependency or a non-Redis
    backend just falls through to the list path rather than raising.
    """
    try:
        from django_redis import get_redis_connection
        return get_redis_connection("default")
    except Exception:
        return None



def get_seen_post_ids(user_id: int) -> set[int]:
    redis = _seen_redis()
    if redis is not None:
        try:
            members = redis.zrange(_seen_zkey(user_id), 0, -1)
            out: set[int] = set()
            for m in members:
                try:
                    out.add(int(m.decode() if isinstance(m, bytes) else m))
                except (ValueError, TypeError, AttributeError):
                    continue
            return out
        except Exception as exc:
            logger.warning(f"[get_seen_post_ids] redis path failed, falling back: {exc}")
    return set(cache.get(_seen_key(user_id)) or [])



def mark_posts_seen(user_id: int, post_ids: Iterable[int]) -> None:
    # Normalize to a clean list of ints once, up front.
    ids = []
    for p in post_ids:
        try:
            ids.append(int(p))
        except (TypeError, ValueError):
            continue
    if not ids:
        return

    redis = _seen_redis()
    if redis is not None:
        try:
            zkey = _seen_zkey(user_id)
            # Millisecond timestamp as the score. All ids in this batch get
            # the same score; later batches score higher, so rank order is
            # oldest→newest and the trim below drops the oldest.
            score = int(timezone.now().timestamp() * 1000)
            mapping = {str(pid): score for pid in ids}

            pipe = redis.pipeline(transaction=True)
            pipe.zadd(zkey, mapping)            # atomic merge, never overwrite
            pipe.expire(zkey, SESSION_DEDUP_TTL_S)
            # Keep only the newest SESSION_DEDUP_MAX_SIZE by rank. Removing
            # ranks [0, -(N+1)] deletes everything except the top N scores.
            pipe.zremrangebyrank(zkey, 0, -(SESSION_DEDUP_MAX_SIZE + 1))
            pipe.execute()
            return
        except Exception as exc:
            logger.warning(f"[mark_posts_seen] redis path failed, falling back: {exc}")

    # Fallback: list-based RMW, serialized within the process by the lock.
    key = _seen_key(user_id)
    with _seen_fallback_lock:
        existing = cache.get(key) or []
        merged = list(dict.fromkeys(list(existing) + ids))
        # Keep the most recent N — drop the oldest if we exceed the cap.
        if len(merged) > SESSION_DEDUP_MAX_SIZE:
            merged = merged[-SESSION_DEDUP_MAX_SIZE:]
        cache.set(key, merged, timeout=SESSION_DEDUP_TTL_S)
