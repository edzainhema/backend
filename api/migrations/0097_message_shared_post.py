# Adds Message.shared_post -- a nullable FK to Post for "share post via DM"
# (the in-app share modal: tap Share on a feed post, pick recipients, the
# resulting message renders as a tappable post-preview bubble). SET_NULL so a
# message survives the underlying post being deleted; the bubble shows a
# "post unavailable" fallback.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0096_activity_session_id_dedupe"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="shared_post",
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="shared_in_messages",
                to="api.post",
            ),
        ),
    ]
