"""
Rolling job: recompute the set of currently-trending hashtags.

Run every few minutes from cron / a scheduler:

    python manage.py build_trending_hashtags

Why this exists
---------------
With the denormalized PostHashtag index in place (ACTIVITY_AND_FEED_AUDIT.md
item A8), "what's trending right now" is a cheap rolling count of how many
posts used each hashtag in the last few minutes. This command computes that
count and writes it to one cache key; the home feed's activity rail reads it
(api.feed.trending.get_trending_hashtags) and amplifies a viewer's affinity for a
hashtag while that tag is hot — see ACTIVITY_AND_FEED_AUDIT.md item D2.

Safe to schedule before anything consumes it: the rail treats a missing /
empty map as "apply no boost", so the feed behaves exactly as before until
the first run, and reverts to that automatically if the job ever stops (the
cache key expires and the multiplier falls back to 1.0).

Cheap and self-contained: a single aggregate query over a tiny time window,
no per-user work, so running it on a tight cadence is fine.
"""

import time

from django.core.cache import cache
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Recompute trending hashtags (rolling window) into the feed cache."

    def handle(self, *args, **opts):
        # Imported here (not at module load) so the command stays importable
        # even if the feed package's heavier imports aren't otherwise needed.
        from api.feed.trending import compute_trending_hashtags
        from api.feed.constants import (
            TRENDING_HASHTAG_KEY,
            TRENDING_HASHTAG_TTL_S,
            TRENDING_HASHTAG_WINDOW_MINUTES,
        )

        t0 = time.time()
        trending = compute_trending_hashtags()

        # Always write — even an empty dict. Storing "{}" records that we
        # checked and nothing qualifies, which stops the request-path
        # self-heal from recomputing on every miss during a quiet window.
        cache.set(TRENDING_HASHTAG_KEY, trending, timeout=TRENDING_HASHTAG_TTL_S)

        top = sorted(trending.items(), key=lambda kv: kv[1], reverse=True)[:5]
        preview = ", ".join(f"#{h}({i:.2f})" for h, i in top) or "(none)"
        self.stdout.write(self.style.SUCCESS(
            f"trending hashtags: {len(trending)} tag(s) over "
            f"{TRENDING_HASHTAG_WINDOW_MINUTES}m window in "
            f"{time.time() - t0:.2f}s — top: {preview}"
        ))
