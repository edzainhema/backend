"""H4: add moderation-triage fields to the three report models.

`PostReport`, `UserReport` and `PageReport` now inherit a shared `ReportTriage`
abstract base (status / handled_by / resolved_at), turning the append-only
report tables into a workable moderation queue: a moderator can mark a row
Reviewing / Actioned / Dismissed, see who handled it and when, and filter the
unhandled ones — instead of an undifferentiated list.

Additive and nullable / defaulted, so existing rows are valid as-is: every
pre-existing report starts `status="open"` with no handler.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


STATUS_CHOICES = [
    ("open", "Open"),
    ("reviewing", "Reviewing"),
    ("actioned", "Actioned — content/account acted on"),
    ("dismissed", "Dismissed — no action needed"),
]


def _triage_fields(handled_related_name):
    return [
        ("status", models.CharField(
            choices=STATUS_CHOICES, db_index=True, default="open", max_length=12,
        )),
        ("handled_by", models.ForeignKey(
            blank=True, null=True,
            on_delete=django.db.models.deletion.SET_NULL,
            related_name=handled_related_name,
            to=settings.AUTH_USER_MODEL,
        )),
        ("resolved_at", models.DateTimeField(blank=True, null=True)),
    ]


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("api", "0101_device_last_seen"),
    ]

    operations = [
        migrations.AddField(model_name=model, name=name, field=field)
        for model, related in (
            ("postreport", "postreport_handled"),
            ("userreport", "userreport_handled"),
            ("pagereport", "pagereport_handled"),
        )
        for name, field in _triage_fields(related)
    ]
