# pg_trgm GIN indexes for the search endpoints (BACKEND_SCALING_AUDIT.md UB-2).
#
# search / search_posts / search_pages / search_message_users filter with
# __icontains (SQL ILIKE \'%q%\'). A leading wildcard defeats a plain B-tree
# index, so those queries full-table-scan and degrade linearly as the user /
# page / post tables grow. A GIN index built with the pg_trgm `gin_trgm_ops`
# operator class DOES accelerate ILIKE \'%q%\' (for queries of >= 3 chars), so
# simply creating these indexes makes the existing __icontains queries
# index-backed -- no query change, no behaviour change (still substring
# containment, not fuzzy similarity).
#
# Postgres-only: pg_trgm / GIN don\'t exist on the SQLite dev fallback (INF-1),
# so the operations no-op there. Built CONCURRENTLY (hence atomic = False) so
# creating them doesn\'t lock the api_post / auth_user tables against writes
# during deploy. IF NOT EXISTS keeps the migration safe to re-run; if a
# CONCURRENTLY build is interrupted it can leave an INVALID index -- DROP it and
# re-run the migration to rebuild.

from django.db import migrations


# (index_name, table, column) -- table/column names are Django defaults
# (no custom db_table on any of these models).
_TRGM_INDEXES = [
    ("user_username_trgm",            "auth_user",       "username"),
    ("userprofile_first_name_trgm",   "api_userprofile", "first_name"),
    ("userprofile_last_name_trgm",    "api_userprofile", "last_name"),
    ("page_name_trgm",                "api_page",        "name"),
    ("post_description_trgm",         "api_post",        "description"),
]


def create_trgm_indexes(apps, schema_editor):
    # Trigram indexes are a PostgreSQL feature; on the SQLite dev fallback this
    # is a no-op (the search endpoints just scan, which is fine for dev data).
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, column in _TRGM_INDEXES:
        schema_editor.execute(
            f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{name}" '
            f'ON "{table}" USING gin ("{column}" gin_trgm_ops)'
        )


def drop_trgm_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for name, _table, _column in _TRGM_INDEXES:
        schema_editor.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
    # Leave the pg_trgm extension in place -- other features may rely on it.


class Migration(migrations.Migration):
    # CONCURRENTLY cannot run inside a transaction, so this migration is not
    # wrapped in one (Django requires atomic = False for that).
    atomic = False

    dependencies = [
        ("api", "0091_userclosefriends"),
    ]

    operations = [
        migrations.RunPython(create_trgm_indexes, drop_trgm_indexes),
    ]
