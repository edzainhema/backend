"""
Nightly job: precompute every active user's taste/affinity profile.

Run from cron / a scheduler once a day (low-traffic hours):

    python manage.py build_affinity_profiles

Why this exists
---------------
The home feed's activity rail ranks candidates against a per-user affinity
profile built from the last 30 days of the Activity stream. That scan is
cheap for a casual user but expensive for a power user (thousands of rows),
and it used to run ON THE REQUEST PATH whenever the cached profile expired.
See ACTIVITY_AND_FEED_AUDIT.md item C4.

This command moves that scan offline: it computes each active user's profile
and writes it to the UserAffinityProfile table. The request path
(api.feed.affinity._build_activity_profile) then just reads the precomputed row —
one indexed lookup, no scan. A user not yet in the table still works (the
request path falls back to an on-demand compute), so the feature is safe to
deploy before the first run.

Bounded and resumable: "active users" are those with any Activity in the last
30 days; users are streamed in chunks; a failure on one user is logged and
skipped rather than aborting the whole run.
"""

import time
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import Activity


ACTIVE_WINDOW_DAYS = 30
USER_CHUNK = 500


class Command(BaseCommand):
    help = "Precompute per-user taste/affinity profiles for the home feed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--active-window-days", type=int, default=ACTIVE_WINDOW_DAYS,
            help="Only (re)build profiles for users active within this window.",
        )

    def handle(self, *args, **opts):
        # Imported here (not at module load) so the command is importable even
        # if the feed package's heavier imports aren't needed elsewhere.
        from api.feed.affinity import (
            _compute_affinity_profile, _store_affinity_profile,
        )

        t0 = time.time()
        since = timezone.now() - timedelta(days=opts["active_window_days"])

        active_ids = list(
            Activity.objects
            .filter(created_at__gte=since)
            .values_list("user_id", flat=True)
            .distinct()
        )
        self.stdout.write(f"active users to profile: {len(active_ids)}")

        built = failed = 0
        for user in (
            User.objects.filter(id__in=active_ids).iterator(chunk_size=USER_CHUNK)
        ):
            try:
                profile = _compute_affinity_profile(user)
                _store_affinity_profile(user, profile)
                # Drop the per-process cache so the fresh profile is read on
                # the next request instead of a stale cached copy.
                cache.delete(f"feed:activity_profile:{user.id}")
                built += 1
            except Exception as exc:
                failed += 1
                self.stderr.write(f"profile failed for user {user.id}: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"built {built} profiles ({failed} failed) in {time.time() - t0:.1f}s"
        ))
