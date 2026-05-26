"""Impression recording: buffered Redis enqueue with a synchronous fallback."""
from __future__ import annotations
import logging

import json
import time

from django.db.models import F

from ..models import Post
from .constants import IMPRESSION_QUEUE_KEY, IMPRESSION_QUEUE_MAX

logger = logging.getLogger(__name__)

# Observability for the silent-degradation failure mode (BACKEND_SCALING_AUDIT.md
# SY-4): the whole point of this finding is that the buffer was inactive and
# nobody noticed. If Redis is configured but unreachable (bad REDIS_URL, outage)
# or the drain falls behind, _enqueue_impressions returns False and every render
# quietly writes on the request path again. We log that — but throttle it so a
# sustained outage produces ~1 line/minute/worker instead of one per render.
# The gate is PROCESS-LOCAL on purpose: it must not depend on the cache, which
# is precisely what may be down.
_ENQUEUE_WARN_INTERVAL_S = 60.0
_last_enqueue_warn = 0.0


def _warn_buffer_inactive(msg: str) -> None:
    global _last_enqueue_warn
    now = time.monotonic()
    if now - _last_enqueue_warn >= _ENQUEUE_WARN_INTERVAL_S:
        _last_enqueue_warn = now
        logger.warning(
            f"[impressions] buffer inactive, writing impressions synchronously: {msg}"
        )

# =============================================================================
# Impression recording (C1) — off the request path via a Redis buffer, with a
# synchronous fallback so it's always correct even without Redis / the drain.
# =============================================================================

def _enqueue_impressions(user_id: int, items: list, session_id: str = "") -> bool:
    """
    Push one render's impressions onto the Redis buffer for the drain job.

    Returns False — telling the caller to write synchronously instead — when
    Redis is unavailable OR the buffer is backed up past IMPRESSION_QUEUE_MAX.
    The backlog check is the self-heal: if the drain is down or unscheduled,
    new renders degrade to synchronous writes rather than growing the buffer
    unbounded or silently dropping data.
    """
    try:
        from django_redis import get_redis_connection
        redis = get_redis_connection("default")
    except Exception as exc:
        # Distinguish a real problem from the documented local-dev mode. When
        # CACHES is the LocMemCache fallback (no REDIS_URL set), the feed
        # fast-paths no-op BY DESIGN — staying silent avoids crying wolf. When
        # the backend IS django_redis but the connection fails, that's the
        # SY-4 failure mode the operator needs to see.
        from django.conf import settings
        backend = settings.CACHES.get("default", {}).get("BACKEND", "")
        if "django_redis" in backend:
            _warn_buffer_inactive(f"redis backend configured but unavailable ({exc})")
        return False
    try:
        if redis.llen(IMPRESSION_QUEUE_KEY) >= IMPRESSION_QUEUE_MAX:
            # Connected fine, but the buffer is full — the drain is down or
            # behind. Renders self-heal to synchronous writes; surface it so the
            # drain can be restarted before the backlog persists.
            _warn_buffer_inactive(
                f"buffer at IMPRESSION_QUEUE_MAX={IMPRESSION_QUEUE_MAX} (drain behind?)"
            )
            return False
        redis.lpush(
            IMPRESSION_QUEUE_KEY,
            json.dumps({"u": user_id, "s": session_id, "items": items}),
        )
        return True
    except Exception as exc:
        _warn_buffer_inactive(f"enqueue failed ({exc})")
        return False



def _write_impressions_sync(user_id: int, items: list, session_id: str = "") -> None:
    """
    The synchronous write path (the original C1 fix): bulk-insert the
    post_impression Activity rows and bump Post.impression_count, on the
    request's own connection. Best-effort — never breaks the response.
    """
    if not items:
        return
    from django.db import transaction
    from ..models import Activity

    rows = [
        Activity(
            user_id=user_id,
            action_type="post_impression",
            post_id=it["post_id"],
            surface="home",
            session_id=session_id,
            metadata={"rail": it.get("rail"), "slot": it.get("slot")},
        )
        for it in items
    ]
    post_ids = [it["post_id"] for it in items]
    try:
        with transaction.atomic():
            Activity.objects.bulk_create(rows, ignore_conflicts=True)
            Post.objects.filter(id__in=post_ids).update(
                impression_count=F("impression_count") + 1
            )
    except Exception as exc:   # pragma: no cover — best-effort analytics
        logger.error(f"[_write_impressions_sync] failed: {exc}")



def record_impressions(user, serialized) -> None:
    """
    Record a feed render's impressions (C1).

    Replaces the per-request daemon thread (which opened its own DB connection
    per request and exhausted the pool under load). Impressions are pushed to a
    Redis buffer and written off the request path by the `drain_impressions`
    command; when Redis is unavailable or backed up, falls back to a
    synchronous write so the data is never lost. Each impression is tagged with
    the request's session id (C3) so "CTR within a visit" is computable.
    """
    if (
        user is None
        or not getattr(user, "is_authenticated", False)
        or not serialized
    ):
        return
    items = [
        {"post_id": d["id"], "rail": d.get("rail"), "slot": i}
        for i, d in enumerate(serialized)
        if d.get("id") is not None
    ]
    if not items:
        return
    from ..session_context import get_current_session_id
    session_id = get_current_session_id()
    if not _enqueue_impressions(user.id, items, session_id):
        _write_impressions_sync(user.id, items, session_id)
