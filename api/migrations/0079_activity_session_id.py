# Adds Activity.session_id (C3) — a per-visit identifier so activity rows can
# be grouped into sessions. See ACTIVITY_AND_FEED_AUDIT.md item C3.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0078_post_impression_count'),
    ]

    operations = [
        migrations.AddField(
            model_name='activity',
            name='session_id',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddIndex(
            model_name='activity',
            index=models.Index(fields=['session_id'], name='api_activit_session_idx'),
        ),
    ]
