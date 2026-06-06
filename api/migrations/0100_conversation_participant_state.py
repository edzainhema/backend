"""M7: per-recipient DM acceptance state.

Adds `ConversationParticipantState(user, conversation, is_accepted,
accepted_at, created_at)`. Backfills one row per existing
(conversation, participant) pair with `is_accepted=True` so every
conversation that exists at deploy time stays in users' main inbox
(grandfathering -- see the rationale in `models/messaging.py`).

The backfill is idempotent: rows are inserted via `bulk_create(
ignore_conflicts=True)` so re-applying after a partial run won't trip
the (user, conversation) unique constraint.
"""
from django.db import migrations, models
import django.db.models.deletion


def _backfill_states(apps, schema_editor):
    """Create an is_accepted=True row for every existing participant.

    Done in chunks to avoid pulling the whole M2M into memory on a
    DB with many conversations. The through-table for the implicit
    `Conversation.participants` M2M is `api_conversation_participants`
    -- we read straight from it via the historical model so this
    works at any future point in the migration history."""
    Conversation = apps.get_model("api", "Conversation")
    ConversationParticipantState = apps.get_model("api", "ConversationParticipantState")

    # Use the through model so we don't have to materialise Conversation
    # instances just to walk their participants. `_meta.through` returns
    # the auto-generated join model for the implicit M2M.
    Through = Conversation.participants.through

    BATCH = 1000
    pending = []
    for row in Through.objects.all().values_list(
        "user_id", "conversation_id"
    ).iterator(chunk_size=BATCH):
        user_id, convo_id = row
        pending.append(ConversationParticipantState(
            user_id=user_id,
            conversation_id=convo_id,
            is_accepted=True,
        ))
        if len(pending) >= BATCH:
            ConversationParticipantState.objects.bulk_create(
                pending, ignore_conflicts=True,
            )
            pending = []

    if pending:
        ConversationParticipantState.objects.bulk_create(
            pending, ignore_conflicts=True,
        )


def _noop_reverse(apps, schema_editor):
    """No-op reverse: dropping the table happens via the schema
    operation below; we don't need to delete the data first."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0099_user_email_phone_uniqueness'),
    ]

    operations = [
        migrations.CreateModel(
            name='ConversationParticipantState',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID',
                )),
                ('is_accepted', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('accepted_at', models.DateTimeField(blank=True, null=True)),
                ('conversation', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='participant_states',
                    to='api.conversation',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='conversation_states',
                    to='auth.user',
                )),
            ],
            options={
                'indexes': [
                    models.Index(
                        fields=['user', 'is_accepted'],
                        name='cps_user_accepted_idx',
                    ),
                ],
                'unique_together': {('user', 'conversation')},
            },
        ),
        migrations.RunPython(_backfill_states, _noop_reverse),
    ]
