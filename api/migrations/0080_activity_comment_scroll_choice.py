# Adds the `comment_scroll` choice to Activity.action_type.
#
# Background: opening a post's comments was tracked, but how FAR the viewer
# scrolled through them wasn't — and scroll depth is a strong "this post is
# genuinely interesting" signal that's independent of liking. The new
# `comment_scroll` action_type carries a depth fraction (0..1) in metadata.
# See ACTIVITY_AND_FEED_AUDIT.md item D1.
#
# This migration only updates the CharField's `choices=` validator. The DB
# column stays a max_length=30 CharField; no schema change, no data backfill
# (this is a brand-new event type with no historical rows to relabel).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0079_activity_session_id'),
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
                    ('comment_scroll', 'Comment Scroll Depth'),
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
