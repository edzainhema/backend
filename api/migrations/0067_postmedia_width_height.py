from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Add nullable width/height columns to PostMedia.

    Captured at upload time so the feed can size each tile before the image
    finishes loading. Existing rows are left NULL; the frontend already
    falls back to Image.getSize() / video naturalSize when these are
    missing.
    """

    dependencies = [
        ("api", "0066_page_chat_media"),
    ]

    operations = [
        migrations.AddField(
            model_name="postmedia",
            name="width",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="postmedia",
            name="height",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
