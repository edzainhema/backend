"""H3: DB-level backstop for the username/email/phone TOCTOU races.

Before this migration only `auth_user.username` had a UNIQUE
constraint -- the existing app-layer `.filter(...).exists()` pre-checks
on email and phone were the *only* barrier. Two simultaneous register
calls with the same email could both pass the check and both insert,
producing silent duplicates. Same for phone via `UserProfile`. This
migration adds the missing partial unique indexes:

  * `auth_user.email`               UNIQUE WHERE email <> ''
  * `api_userprofile.phone_number`  UNIQUE WHERE phone_number <> ''

Both are CONDITIONAL on non-empty because the register flow leaves
these fields blank when the user signs up with the other identifier
type. Two users with blank email / blank phone is fine; two users
with the same NON-blank value is the bug.

We don't need a `LOWER(email)` functional index because every call
site (register_user, social_auth, update_profile_settings) stores
email lowercased -- a plain-equality unique index is sufficient.
Same logic for phone, which `_normalize_phone` strips to a canonical
form before storage.

Pre-existing data: if the database already has duplicate non-empty
emails or phones, this migration will FAIL with an IntegrityError on
apply. In a pre-launch DB that should be empty / clean. If you hit
this post-launch, dedupe the offending rows first (keep oldest,
clear the rest) before re-applying.

Why a RunPython wrapper for the email constraint: `AddConstraint`
resolves `model_name` within the migration's own app, so it can't be
pointed at `auth.User`. We invoke `schema_editor.add_constraint` ourselves
with the real `User` class -- that's the same code path `AddConstraint`
uses for the userprofile_phone_uniq constraint just above, so vendor
quirks (SQLite vs Postgres partial-index syntax) are handled by Django
identically for both. The earlier raw `CREATE UNIQUE INDEX ... WHERE ...`
form silently failed to enforce on SQLite even though the migration log
reported it as applied; routing through schema_editor avoids whatever
parsing quirk caused that.
"""
from django.db import migrations, models
from django.db.models import Q


def _add_user_email_unique(apps, schema_editor):
    """Create the partial unique index on auth_user.email via the
    schema editor -- same DDL path AddConstraint uses for the phone
    index, just applied to a model in another app."""
    # Import the REAL User class (not the historical one from `apps`)
    # because schema_editor.add_constraint reads `Meta.db_table` and
    # constructs raw SQL; the historical version is fine for either
    # but the real one is more legible.
    from django.contrib.auth.models import User
    constraint = models.UniqueConstraint(
        fields=['email'],
        condition=~Q(email=''),
        name='auth_user_email_uniq',
    )
    schema_editor.add_constraint(User, constraint)


def _remove_user_email_unique(apps, schema_editor):
    from django.contrib.auth.models import User
    constraint = models.UniqueConstraint(
        fields=['email'],
        condition=~Q(email=''),
        name='auth_user_email_uniq',
    )
    schema_editor.remove_constraint(User, constraint)


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0098_post_tag_notification_type'),
        # Explicit auth dep so the auth_user table exists before
        # _add_user_email_unique runs against it.
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='userprofile',
            constraint=models.UniqueConstraint(
                fields=['phone_number'],
                condition=~Q(phone_number=''),
                name='userprofile_phone_uniq',
            ),
        ),
        migrations.RunPython(
            _add_user_email_unique,
            _remove_user_email_unique,
            # No state change -- we don't track the constraint in
            # api's model state because the model lives in auth.
            elidable=False,
        ),
    ]
