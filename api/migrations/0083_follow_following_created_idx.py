# Generated for keyset pagination of the followers list (list_my_followers).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0082_pagechatmessage_reply_to'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='follow',
            index=models.Index(
                fields=['following', '-created_at', '-id'],
                name='follow_following_created_idx',
            ),
        ),
    ]
