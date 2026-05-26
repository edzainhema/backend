"""
Adds media support to page chat:
  * PageChatMessage.text becomes blank=True (so media-only messages save).
  * PageChatMessage gains `media` (FileField) + `media_type`.
  * New `PageChatMessageMedia` model holds multi-attachment fan-out.

Mirrors the structure used by the DM Message / MessageMedia pair.

Numbered 0066 rather than 0064 because 0064 was already taken by the
userprofile location migration; the current leaf is 0065.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0065_post_upload_location"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pagechatmessage",
            name="text",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="pagechatmessage",
            name="media",
            field=models.FileField(blank=True, null=True, upload_to="page_chat_media/"),
        ),
        migrations.AddField(
            model_name="pagechatmessage",
            name="media_type",
            field=models.CharField(
                blank=True,
                choices=[("image", "Image"), ("video", "Video"), ("audio", "Audio")],
                max_length=10,
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="PageChatMessageMedia",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("file", models.FileField(upload_to="page_chat_media/")),
                (
                    "media_type",
                    models.CharField(
                        choices=[
                            ("image", "Image"),
                            ("video", "Video"),
                            ("audio", "Audio"),
                        ],
                        max_length=10,
                    ),
                ),
                ("order", models.PositiveSmallIntegerField(default=0)),
                (
                    "message",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="media_items",
                        to="api.pagechatmessage",
                    ),
                ),
            ],
            options={
                "ordering": ["order"],
                "indexes": [
                    models.Index(
                        fields=["message"], name="api_pagecha_message_idx"
                    ),
                ],
            },
        ),
    ]
