# Adds Page.event_address: the full formatted address for a picked place,
# kept separate from event_location (which now holds the short display name).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0086_page_event_latitude_longitude_place_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='page',
            name='event_address',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
