"""
Notification badge cache helpers.

Lives at the api package level (not inside views/) so apps.py:ready() can
import it without triggering views/__init__.py — which fans out to every
view module and is therefore a bad place to depend on from app startup.

The home-header bell badge re-fetches /notifications/unread-count/ on every
screen focus, which previously fired ~2 DB queries per focus. We cache the
result per user for 30 s and invalidate it on the three paths that change
the count:
    - Notification.objects.create(...)  → post_save signal in apps.py
    - mark_notification_read / mark_all_notifications_read in views.notifications
    - the page-invite read in views.pages
"""

from django.core.cache import cache


UNREAD_COUNT_CACHE_TTL_S = 30


def _unread_count_cache_key(user_id: int) -> str:
    return f"unread_notif_count:{user_id}"


def invalidate_unread_count_cache(user_id: int) -> None:
    """Callers that mark notifications read should call this."""
    cache.delete(_unread_count_cache_key(user_id))
