# Adds the NotInterested model (the "show me less of this" signal) and the
# matching `not_interested` Activity action choice. See
# ACTIVITY_AND_FEED_AUDIT.md item B2.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0074_lowercase_activity_search_queries'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='NotInterested',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(
                    choices=[('post', 'Post'), ('author', 'Author'), ('topic', 'Topic')],
                    max_length=10,
                )),
                ('hashtag', models.CharField(blank=True, default='', max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('post', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='+', to='api.post',
                )),
                ('target_user', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='+', to=settings.AUTH_USER_MODEL,
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='not_interested', to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
        migrations.AddIndex(
            model_name='notinterested',
            index=models.Index(fields=['user', 'kind'], name='api_notint_user_kind_idx'),
        ),
        migrations.AddConstraint(
            model_name='notinterested',
            constraint=models.UniqueConstraint(
                condition=models.Q(kind='post'),
                fields=('user', 'post'),
                name='uniq_notinterested_post',
            ),
        ),
        migrations.AddConstraint(
            model_name='notinterested',
            constraint=models.UniqueConstraint(
                condition=models.Q(kind='author'),
                fields=('user', 'target_user'),
                name='uniq_notinterested_author',
            ),
        ),
        migrations.AddConstraint(
            model_name='notinterested',
            constraint=models.UniqueConstraint(
                condition=models.Q(kind='topic'),
                fields=('user', 'hashtag'),
                name='uniq_notinterested_topic',
            ),
        ),
        migrations.AlterField(
            model_name='activity',
            name='action_type',
            field=models.CharField(
                max_length=30,
                choices=[
                    ('post_view', 'Post View'),
                    ('post_impression', 'Post Impression'),
                    ('post_dwell', 'Post Dwell'),
                    ('post_like', 'Post Like'),
                    ('post_unlike', 'Post Unlike'),
                    ('post_save', 'Post Save'),
                    ('post_unsave', 'Post Unsave'),
                    ('post_share', 'Post Share'),
                    ('post_comment', 'Post Comment'),
                    ('user_visit', 'User Profile Visit'),
                    ('page_visit', 'Page Visit'),
                    ('reel_watch', 'Reel Watch'),
                    ('reel_complete', 'Reel Watched To End'),
                    ('reel_rewatch', 'Reel Rewatch'),
                    ('reel_skip', 'Reel Skip'),
                    ('search_query', 'Search Query'),
                    ('search_click', 'Search Result Click'),
                    ('hashtag_engage', 'Hashtag Engagement'),
                    ('tab_view', 'Tab Viewed'),
                    ('not_interested', 'Not Interested'),
                ],
            ),
        ),
    ]
