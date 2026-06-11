from django.db import migrations, models


class Migration(migrations.Migration):
    """Soft delete for pages: add Page.deleted_at and point both Page and Post
    at their unfiltered `all_objects` manager for related-object access, so
    trashing a page (setting deleted_at) hides it and its posts via the default
    managers while FK traversal still resolves the trashed page/post."""

    dependencies = [
        ("api", "0105_postmedia_hls_master"),
    ]

    operations = [
        migrations.AddField(
            model_name="page",
            name="deleted_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AlterModelOptions(
            name="page",
            options={"base_manager_name": "all_objects"},
        ),
        migrations.AlterModelOptions(
            name="post",
            options={"base_manager_name": "all_objects"},
        ),
    ]
