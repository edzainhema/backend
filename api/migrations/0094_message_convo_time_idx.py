# DM Message composite index (BACKEND_SCALING_AUDIT.md IX-2): serves the thread
# load (filter conversation, order by -created_at) and list_conversations' per-
# conversation latest-message / unread aggregates. Plain b-tree, portable across
# PostgreSQL and the SQLite dev fallback. Mirrors PageChatMessage's index.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0093_notification_indexes"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="message",
            index=models.Index(
                fields=["conversation", "created_at", "id"],
                name="message_convo_time_idx",
            ),
        ),
    ]
