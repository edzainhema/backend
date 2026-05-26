# Adds structured location fields to Page: latitude/longitude/place_id,
# captured when an owner picks a Google Places suggestion in LocationModal.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0085_muteduser_pageinvite_pageposter_keyset_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='page',
            name='event_latitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='page',
            name='event_longitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='page',
            name='event_place_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
