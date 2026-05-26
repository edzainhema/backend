# Adds UserCloseFriends -- the precomputed "very close friends" set read on the
# request path, written nightly by build_close_friends (BACKEND_SCALING_AUDIT.md
# item UB-1). Mirrors UserAffinityProfile (migration 0077).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0090_userprofile_email_verified'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='UserCloseFriends',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('friend_ids', models.JSONField(default=list)),
                ('built_at', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='close_friends',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
    ]
