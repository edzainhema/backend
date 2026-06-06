"""H3: add Device.last_seen + backfill, to enable reaping stale push tokens.

An FCM registration token is unique per app INSTALL, not per account, so several
accounts on one phone share a token. If an account logs out but its client
`unregister-device` call never lands (crash / offline / force-quit), the stale
(account, token) row keeps delivering that account's notifications to whoever now
holds the device — a cross-account push leak.

`last_seen` is bumped every time a (user, token) re-registers (login / app
foreground; see views/devices.py:register_device). A logged-out account stops
re-registering, so its row ages out and `prune_stale_devices` can delete it,
while a still-signed-in account keeps refreshing and survives. This is the
multi-account-safe alternative to a blind cross-account delete on register
(which can't tell "A logged out" from "A and B are both signed in").

Backfills existing rows to their `created_at` so legacy tokens get a sensible
initial age instead of all looking brand-new at deploy time.
"""
from django.db import migrations, models
from django.db.models import F


def _backfill_last_seen(apps, schema_editor):
    Device = apps.get_model("api", "Device")
    # One UPDATE: seed last_seen from created_at for every pre-existing row.
    Device.objects.filter(last_seen__isnull=True).update(last_seen=F("created_at"))


def _noop_reverse(apps, schema_editor):
    # Nothing to undo on reverse beyond dropping the column (handled by the
    # AddField reversal); the backfilled values disappear with it.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0100_conversation_participant_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="device",
            name="last_seen",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(_backfill_last_seen, _noop_reverse),
    ]
