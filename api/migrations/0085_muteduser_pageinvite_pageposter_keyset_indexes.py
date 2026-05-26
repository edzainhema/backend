# Composite indexes backing keyset pagination of the muted-users
# (list_muted_users), sent-page-invites (list_sent_page_invites) and
# allowed-posters (get_page_posters) lists. Each mirrors the index already
# added for blocked-users/page-followers in 0084.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0084_pagefollow_blockeduser_keyset_indexes'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='muteduser',
            index=models.Index(
                fields=['user', '-created_at', '-id'],
                name='muteduser_user_created_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='pageinvite',
            index=models.Index(
                fields=['page', '-created_at', '-id'],
                name='pageinvite_page_created_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='pageposter',
            index=models.Index(
                fields=['page', '-added_at', '-id'],
                name='pageposter_page_added_idx',
            ),
        ),
    ]
