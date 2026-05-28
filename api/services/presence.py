"""User presence: throttled last_seen write (WS-2)."""

# ── Presence: throttled last_seen write (WS-2) ──────────────────────────────
# last_seen otherwise gets written on every authenticated HTTP request
# (UpdateLastSeenMiddleware) and every inbound WebSocket frame (the chat
# consumers) — a write to the hot UserProfile row on essentially every user
# action. We throttle it behind a short-lived cache marker so the DB write
# happens at most once per LAST_SEEN_THROTTLE_S per user. is_online uses a
# 3-minute window, so a marker up to ~45s old still keeps presence accurate.
LAST_SEEN_THROTTLE_S = 45


def touch_last_seen(user_id):
    """Best-effort, throttled bump of UserProfile.last_seen for ``user_id``.

    Uses ``cache.add()`` (atomic check-and-set): it returns True only for the
    first caller within the TTL window, so exactly one DB write happens per
    window and concurrent callers can't race into duplicate writes the way a
    ``get()`` + ``set()`` pair can. Never raises — presence must never break a
    request or a WebSocket frame.
    """
    if not user_id:
        return
    try:
        from django.core.cache import cache

        # add() succeeds (True) only when no marker exists yet → do the write
        # and open the window. While the marker lives, every later call is a
        # single cache hit with no DB write.
        if cache.add(f"last_seen:{user_id}", 1, timeout=LAST_SEEN_THROTTLE_S):
            from django.utils import timezone

            from ..models import UserProfile

            UserProfile.objects.filter(user_id=user_id).update(
                last_seen=timezone.now()
            )
    except Exception:
        pass  # presence is best-effort — never break the caller


