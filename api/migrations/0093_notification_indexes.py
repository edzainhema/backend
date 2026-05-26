# Notification composite indexes (BACKEND_SCALING_AUDIT.md IX-1): one for the
# ordered list (filter recipient, order by -created_at) and one for the unread
# bell-badge count (filter recipient, is_read). Plain b-tree indexes -- portable
# across PostgreSQL and the SQLite dev fallback.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0092_search_trgm_indexes"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(
                fields=["recipient", "-created_at"],
                name="notif_recipient_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(
                fields=["recipient", "is_read"],
                name="notif_recipient_unread_idx",
            ),
        ),
    ]
