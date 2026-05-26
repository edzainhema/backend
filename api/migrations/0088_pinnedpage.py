# Generated for the PinnedPage feature — pages a user pins to their profile.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0087_page_event_address'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PinnedPage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('page', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pinned_by', to='api.page')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pinned_pages', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'unique_together': {('user', 'page')},
            },
        ),
        migrations.AddIndex(
            model_name='pinnedpage',
            index=models.Index(fields=['user', '-created_at', '-id'], name='pinnedpage_user_created_idx'),
        ),
    ]
