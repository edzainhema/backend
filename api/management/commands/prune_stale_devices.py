"""Reap stale push-notification Device rows (audit H3 / L7).

A Device row is "stale" when its account stopped re-registering the token — the
signal of a logged-out (or uninstalled) account whose client
`unregister-device` call never landed. An FCM token is unique per app INSTALL,
not per account, so a stale row keeps delivering that account's push
notifications to whoever now holds the physical device: a cross-account leak.

`register_device` bumps `Device.last_seen` on every login / app foreground, so a
still-signed-in account keeps its row fresh while a logged-out one ages out.
This command deletes rows not seen within --max-age-days (default 60) — the
proactive complement to:
  * the client's own `unregister-device` call (the fast path), and
  * FCM's dead-token pruning in services/push.py (UnregisteredError etc.).

Multi-account safe: it removes only rows the OWNING account itself stopped
refreshing, keyed per (user, token). It NEVER does a token-wide delete, so the
legitimate "two accounts on one phone" case — where the live account keeps
registering — is untouched even if a sibling row for the same token is reaped.

Schedule daily (see deploy/cron/prune-stale-devices.cron). Idempotent and
crash-tolerant: a row deleted twice is a no-op the second time.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from api.models import Device

DEFAULT_MAX_AGE_DAYS = 60


class Command(BaseCommand):
    help = "Delete push Device rows not re-registered within --max-age-days (H3)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-age-days",
            type=int,
            default=DEFAULT_MAX_AGE_DAYS,
            help=(
                "Reap rows last seen more than N days ago "
                f"(default {DEFAULT_MAX_AGE_DAYS})."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many rows WOULD be reaped without deleting.",
        )

    def handle(self, *args, **opts):
        max_age_days = opts["max_age_days"]
        if max_age_days < 1:
            # A 0/negative window would reap live rows — refuse rather than
            # nuke every device token.
            self.stderr.write("--max-age-days must be >= 1; refusing to run.")
            return
        cutoff = timezone.now() - timedelta(days=max_age_days)

        # Reap by last_seen; fall back to created_at for any legacy row whose
        # last_seen was never set (shouldn't happen after the 0101 backfill, but
        # be defensive so a stray NULL can't make a row immortal).
        stale = Device.objects.filter(
            Q(last_seen__lt=cutoff)
            | Q(last_seen__isnull=True, created_at__lt=cutoff)
        )

        if opts["dry_run"]:
            n = stale.count()
            self.stdout.write(
                f"[dry-run] would reap {n} stale Device row(s) "
                f"older than {max_age_days}d"
            )
            return

        deleted, _ = stale.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"reaped {deleted} stale Device row(s) not seen in {max_age_days}d"
            )
        )
