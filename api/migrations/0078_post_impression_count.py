# Adds Post.impression_count (the fast-read CTR denominator) and backfills it
# from the existing post_impression Activity rows so engagement-rate ranking
# works from day one, not just for impressions logged after deploy.
# See ACTIVITY_AND_FEED_AUDIT.md item C5.

from django.db import migrations, models


def backfill_impression_counts(apps, schema_editor):
    Activity = apps.get_model("api", "Activity")
    Post = apps.get_model("api", "Post")
    from django.db.models import Count

    counts = (
        Activity.objects
        .filter(action_type="post_impression", post_id__isnull=False)
        .values("post_id")
        .annotate(c=Count("id"))
        .order_by()
    )
    for row in counts.iterator(chunk_size=5000):
        Post.objects.filter(id=row["post_id"]).update(impression_count=row["c"])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0077_useraffinityprofile'),
    ]

    operations = [
        migrations.AddField(
            model_name='post',
            name='impression_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(
            backfill_impression_counts,
            migrations.RunPython.noop,   # reverse: leave the column, nothing to undo
        ),
    ]
