# Comment (post, created_at) composite index (BACKEND_SCALING_AUDIT.md IX-3):
# get_comments filters by post and orders by created_at; this avoids the
# per-post filesort the post FK index alone can't. Plain b-tree, portable
# across PostgreSQL and the SQLite dev fallback.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0094_message_convo_time_idx"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="comment",
            index=models.Index(
                fields=["post", "created_at"],
                name="comment_post_created_idx",
            ),
        ),
    ]
