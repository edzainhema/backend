# Adds the `post_impression` choice to Activity.action_type.
#
# Background: home-feed impression rows were being written with
# action_type="post_dwell" (with no duration), which collided with the
# real client-side post_dwell event (which always has a duration). The
# fix introduces a distinct `post_impression` action_type. See
# ACTIVITY_AND_FEED_AUDIT.md item A2.
#
# This migration only updates the CharField's `choices=` validator. The
# DB column stays a max_length=30 CharField; no schema change. A
# follow-up data migration (0071_relabel_impressions) re-labels existing
# historical rows.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0069_device_unique_together'),
    ]

    operations = [
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
                ],
            ),
        ),
    ]
