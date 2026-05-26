# Generated for the Activity tracking model.

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0058_alter_profilevisit_surface_alter_videowatch_surface_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Activity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action_type', models.CharField(choices=[
                    ('post_view', 'Post View'),
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
                ], max_length=30)),
                ('duration_seconds', models.FloatField(blank=True, null=True)),
                ('surface', models.CharField(blank=True, choices=[
                    ('home', 'Home Feed'),
                    ('reels', 'Reels'),
                    ('explore', 'Explore'),
                    ('profile', 'Profile'),
                    ('page', 'Page'),
                    ('comments', 'Comments'),
                    ('messages', 'Messages'),
                    ('notifications', 'Notifications'),
                    ('search', 'Search Suggestions'),
                    ('search_results', 'Search Results'),
                    ('history', 'Search History'),
                    ('post_detail', 'Post Detail'),
                ], default='', max_length=20)),
                ('channel', models.CharField(blank=True, choices=[
                    ('search', 'Search'),
                    ('direct', 'Direct'),
                    ('link', 'Link'),
                    ('feed', 'Feed'),
                ], default='', max_length=20)),
                ('tab', models.CharField(blank=True, default='', max_length=30)),
                ('watched_to_end', models.BooleanField(default=False)),
                ('is_rewatch', models.BooleanField(default=False)),
                ('is_skip', models.BooleanField(default=False)),
                ('query', models.CharField(blank=True, default='', max_length=255)),
                ('sentiment_label', models.CharField(blank=True, choices=[
                    ('positive', 'Positive'),
                    ('neutral', 'Neutral'),
                    ('negative', 'Negative'),
                    ('question', 'Question'),
                    ('intent_buy', 'Purchase Intent'),
                    ('mixed', 'Mixed'),
                ], default='', max_length=20)),
                ('sentiment_score', models.FloatField(blank=True, null=True)),
                ('keywords', models.JSONField(blank=True, default=list)),
                ('hashtag', models.CharField(blank=True, default='', max_length=100)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('comment', models.ForeignKey(blank=True, null=True,
                    on_delete=models.deletion.CASCADE,
                    related_name='activities', to='api.comment')),
                ('page', models.ForeignKey(blank=True, null=True,
                    on_delete=models.deletion.CASCADE,
                    related_name='activities', to='api.page')),
                ('post', models.ForeignKey(blank=True, null=True,
                    on_delete=models.deletion.CASCADE,
                    related_name='activities', to='api.post')),
                ('target_user', models.ForeignKey(blank=True, null=True,
                    on_delete=models.deletion.CASCADE,
                    related_name='activities_targeting_me', to=settings.AUTH_USER_MODEL)),
                ('user', models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name='activities', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='activity',
            index=models.Index(fields=['user', '-created_at'], name='api_activit_user_id_a1f7e3_idx'),
        ),
        migrations.AddIndex(
            model_name='activity',
            index=models.Index(fields=['user', 'action_type', '-created_at'], name='api_activit_user_id_b2c8f4_idx'),
        ),
        migrations.AddIndex(
            model_name='activity',
            index=models.Index(fields=['action_type', '-created_at'], name='api_activit_action__c3d9a5_idx'),
        ),
        migrations.AddIndex(
            model_name='activity',
            index=models.Index(fields=['post'], name='api_activit_post_id_d4e0b6_idx'),
        ),
        migrations.AddIndex(
            model_name='activity',
            index=models.Index(fields=['page'], name='api_activit_page_id_e5f1c7_idx'),
        ),
        migrations.AddIndex(
            model_name='activity',
            index=models.Index(fields=['target_user'], name='api_activit_target__f6a2d8_idx'),
        ),
        migrations.AddIndex(
            model_name='activity',
            index=models.Index(fields=['hashtag'], name='api_activit_hashtag_a7b3e9_idx'),
        ),
        migrations.AddIndex(
            model_name='activity',
            index=models.Index(fields=['sentiment_label'], name='api_activit_sentime_b8c4fa_idx'),
        ),
    ]
