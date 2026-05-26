"""
Nightly job: precompute every active user's "very close friends" set (UB-1).

Run from cron / a scheduler once a day (low-traffic hours):

    python manage.py build_close_friends

Why this exists
---------------
The home feed's friend-network rail (B8) weights authors followed by the
viewer's close friends. That set comes from get_very_close_friend_ids, which
scans the last 30 days of the viewer's DMs, tags, comments, and likes -- cheap
for a casual user but, for a creator whose posts attract thousands of
likes/comments a day, tens of thousands of rows pulled into memory. It used to
run ON THE REQUEST PATH on every build_feed_context cache miss (per worker,
every 90s). See BACKEND_SCALING_AUDIT.md item UB-1.

This command moves that scan offline: it computes each active user's set and
writes it to UserCloseFriends. The request path
(services.feed_helpers.get_close_friend_ids) then just reads the precomputed
row. A user not yet in the table still works (the read path falls back to a
bounded on-demand compute), so the feature is safe to deploy before the first
run. Mirrors build_affinity_profiles.
"""

import time
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import Activity, UserCloseFriends


ACTIVE_WINDOW_DAYS = 30
USER_CHUNK = 500


class Command(BaseCommand):
    help = "Precompute each active user's close-friends set for the feed (UB-1)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--active-window-days", type=int, default=ACTIVE_WINDOW_DAYS,
            help="Only (re)build sets for users active within this window.",
        )

    def handle(self, *args, **opts):
        # Imported here (not at module load) to keep the command importable
        # without dragging in the feed package's heavier imports.
        from api.services.feed_helpers import get_very_close_friend_ids

        t0 = time.time()
        since = timezone.now() - timedelta(days=opts["active_window_days"])

        active_ids = list(
            Activity.objects
            .filter(created_at__gte=since)
            .values_list("user_id", flat=True)
            .distinct()
        )
        self.stdout.write(f"active users to process: {len(active_ids)}")

        built = failed = 0
        for user in (
            User.objects.filter(id__in=active_ids).iterator(chunk_size=USER_CHUNK)
        ):
            try:
                ids = get_very_close_friend_ids(user)
                UserCloseFriends.objects.update_or_create(
                    user=user, defaults={"friend_ids": sorted(ids)}
                )
                built += 1
            except Exception as exc:
                failed += 1
                self.stderr.write(f"close-friends failed for user {user.id}: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"built {built} close-friend sets ({failed} failed) in {time.time() - t0:.1f}s"
        ))
