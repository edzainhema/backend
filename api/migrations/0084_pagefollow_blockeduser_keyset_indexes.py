# Composite indexes backing keyset pagination of the page-followers
# (get_page_followers) and blocked-users (list_blocked_users) lists.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0083_follow_following_created_idx'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='pagefollow',
            index=models.Index(
                fields=['page', '-created_at', '-id'],
                name='pagefollow_page_created_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='blockeduser',
            index=models.Index(
                fields=['user', '-created_at', '-id'],
                name='blockeduser_user_created_idx',
            ),
        ),
    ]
