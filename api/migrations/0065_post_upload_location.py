# Adds three nullable columns to Post for the device's GPS coordinates
# at the moment the user hit Share. Captured by ProcessUpload.tsx via
# the cached/fresh fix from frontend/src/utils/permissions.ts, then
# persisted by api/views.create_post.
#
# All fields default to NULL so this migration is safe to apply to a
# populated posts table without backfill — posts created before the
# feature shipped, or by users who declined location permission, just
# stay NULL and the feed-ranker falls back to non-geo ranking for them.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0064_userprofile_location_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='post',
            name='upload_latitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='post',
            name='upload_longitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='post',
            name='upload_accuracy_m',
            field=models.FloatField(blank=True, null=True),
        ),
    ]
