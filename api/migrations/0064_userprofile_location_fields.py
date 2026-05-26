# Generated for the first-launch location personalization feature.
#
# Adds the four nullable columns we use to remember each user's most
# recently reported device location. All fields default to NULL, so the
# migration is safe to apply to a populated table without backfill —
# users who never grant the location permission simply stay NULL and
# fall back to non-geo ranking in the feed.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0063_alter_pagechatmessage_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='latitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='longitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='location_accuracy_m',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='location_updated_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
