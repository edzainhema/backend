import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0081_rename_api_activit_session_idx_api_activit_session_53130e_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='pagechatmessage',
            name='reply_to',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='replies', to='api.pagechatmessage'),
        ),
    ]
