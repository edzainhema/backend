import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Post-level soft delete (Phase 1): add Post.trashed_at / trashed_reason
    and change Post.page from CASCADE to SET_NULL so a post can outlive (and
    sit in trash after) the deletion of its page.

    Chained after 0107_alter_page_managers_alter_post_managers (Django's
    auto-generated manager migration) so the two stay linear instead of forking
    the graph into two 0107 leaves."""

    dependencies = [
        ("api", "0107_alter_page_managers_alter_post_managers"),
    ]

    operations = [
        migrations.AddField(
            model_name="post",
            name="trashed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="post",
            name="trashed_reason",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AlterField(
            model_name="post",
            name="page",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="posts",
                to="api.page",
            ),
        ),
    ]
