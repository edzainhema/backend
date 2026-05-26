# Adds UserAffinityProfile — the precomputed taste profile read on the
# request path, written nightly by build_affinity_profiles
# (ACTIVITY_AND_FEED_AUDIT.md item C4).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0076_recommendedauthor'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='UserAffinityProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('data', models.JSONField(default=dict)),
                ('built_at', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='affinity_profile',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
    ]
