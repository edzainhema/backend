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

    class Meta:
        # One row per (user, token). Without this constraint, register_device
        # could only key by user (overwriting the previous device's token
        # whenever the user logged into a second phone). With the (user,
        # token) key, each physical device gets its own row, and the same
        # token re-registered idempotently updates rather than duplicates.
        unique_together = ('user', 'token')

    def __str__(self):
        return f"{self.user.username} - {self.token[:20]}"
