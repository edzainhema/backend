# Drop the duplicate session_id index on Activity (BACKEND_SCALING_AUDIT.md IX-4).
#
# session_id carried BOTH a Meta Index(fields=["session_id"])
# (api_activit_session_53130e_idx, from 0079 + the 0081 rename) AND a field-level
# db_index=True (added by 0081's AlterField) -- two indexes on one column on a
# write-heavy, append-only table, paid on every insert. This AlterField removes
# the field-level db_index (the reverse of 0081), leaving the Meta index. Django
# drops the implicit field index (api_activity_session_id_*) and keeps the named
# Meta index.
#
# created_at is intentionally untouched: its db_index is the ONLY standalone
# created_at index (the Meta entries are all composites) and is used by the
# nightly created_at-range jobs, so it is not a duplicate.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0095_comment_post_created_idx"),
    ]

    operations = [
        migrations.AlterField(
            model_name="activity",
            name="session_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
