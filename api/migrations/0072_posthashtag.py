# Creates the PostHashtag denormalized index.
#
# Replaces the feed ranker's old `description__icontains="#tag"` substring
# scan (imprecise — "#blue" matched "#blueberry" — and unindexed) with an
# exact, indexed tag->post lookup. See ACTIVITY_AND_FEED_AUDIT.md item A8.
# A follow-up data migration (0073_backfill_post_hashtags) populates rows
# for existing posts.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0071_relabel_impressions'),
    ]

    operations = [
        migrations.CreateModel(
            name='PostHashtag',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('hashtag', models.CharField(max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('post', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='hashtags',
                    to='api.post',
                )),
            ],
        ),
        migrations.AddConstraint(
            model_name='posthashtag',
            constraint=models.UniqueConstraint(
                fields=('post', 'hashtag'), name='uniq_post_hashtag'
            ),
        ),
        migrations.AddIndex(
            model_name='posthashtag',
            index=models.Index(
                fields=['hashtag', 'post'], name='posthashtag_tag_post_idx'
            ),
        ),
    ]
