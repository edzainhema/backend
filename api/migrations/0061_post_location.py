from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0060_rename_api_activit_user_id_a1f7e3_idx_api_activit_user_id_3893a3_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="post",
            name="location",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
