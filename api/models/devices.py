# Auto-split from the former monolithic api/models.py by domain.
# All models keep app_label 'api' and identical fields, so this split is
# migration-neutral (verified via `makemigrations --check`). Re-exported
# from api/models/__init__.py so `from api.models import X` still works.

from django.db import models
from django.contrib.auth.models import User


class Device(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='devices')
    token = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    # H3: bumped to "now" every time this (user, token) re-registers — which the
    # client does on each login and app foreground. A logged-out account stops
    # re-registering, so its row goes stale and `prune_stale_devices` reaps it;
    # a still-signed-in account keeps refreshing, so its row survives. This is
    # the freshness signal that lets a stale token (whose client
    # `unregister-device` never landed) age out, closing the cross-account
    # push-leak window WITHOUT a blind cross-account delete that would evict an
    # account still signed in on a shared device. Nullable + indexed: legacy
    # rows are backfilled to `created_at` in migration 0101; the reaper filters
    # on it.
    last_seen = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        # One row per (user, token). Without this constraint, register_device
        # could only key by user (overwriting the previous device's token
        # whenever the user logged into a second phone). With the (user,
        # token) key, each physical device gets its own row, and the same
        # token re-registered idempotently updates rather than duplicates.
        unique_together = ('user', 'token')

    def __str__(self):
        return f"{self.user.username} - {self.token[:20]}"
