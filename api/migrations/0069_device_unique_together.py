# Migration: enforce one Device row per (user, token).
#
# Before this, the Device model had no uniqueness constraint at all,
# which let `register_device` (with its old user-only update_or_create
# key) silently overwrite a user's previous device token whenever they
# logged into a second phone — kicking the first phone off push.
#
# Matches the constraint already on DeviceToken (see migration 0008).
# Safe to apply against existing data: under the previous view code
# there was at most one Device row per user, so no (user, token)
# duplicates can exist for this constraint to conflict with.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0068_merge_20260516_2206'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='device',
            unique_together={('user', 'token')},
        ),
    ]
