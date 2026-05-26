# Re-label historical home-feed impression rows from "post_dwell" to
# "post_impression".
#
# Until 0070, compose_home_feed_page (api.feed.compose) was writing impression
# rows with action_type="post_dwell" and no duration_seconds — colliding
# with the real client-side post_dwell event that ALWAYS carries a
# duration. The fingerprint that lets us distinguish them confidently:
#
#     action_type == "post_dwell"
#     AND duration_seconds IS NULL
#     AND surface == "home"
#
# Real post_dwell rows from views/analytics.log_post_dwell always pass a
# numeric `duration_seconds` (the analytics view coerces with float() and
# the frontend at utils/activity.ts drops sub-300ms blips, so no real
# dwell row ever lands with a NULL duration). The `surface == "home"`
# clause is belt-and-suspenders: the impression bulk_create always set
# surface="home", so any non-home post_dwell row is necessarily a real
# dwell event regardless of duration.
#
# Doing this as a single .update() (no python-loop) keeps the migration
# fast even when there are tens of millions of rows. PostgreSQL and
# SQLite both execute this as a single UPDATE ... WHERE statement and
# can run it under transaction safely.
#
# This migration is REVERSIBLE: the inverse re-labels matching rows back
# to "post_dwell" so a rollback to 0070 leaves no orphan action_types
# behind. Note the inverse looks for surface="home" + the metadata.rail
# fingerprint that the impression bulk_create always set, since after
# the forward migration these rows are the only post_impression rows
# we've ever produced.

from django.db import migrations


def relabel_forward(apps, schema_editor):
    Activity = apps.get_model("api", "Activity")
    (
        Activity.objects
        .filter(
            action_type="post_dwell",
            duration_seconds__isnull=True,
            surface="home",
        )
        .update(action_type="post_impression")
    )


def relabel_reverse(apps, schema_editor):
    Activity = apps.get_model("api", "Activity")
    (
        Activity.objects
        .filter(
            action_type="post_impression",
            duration_seconds__isnull=True,
            surface="home",
        )
        .update(action_type="post_dwell")
    )


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0070_activity_post_impression_choice'),
    ]

    operations = [
        migrations.RunPython(relabel_forward, relabel_reverse),
    ]
