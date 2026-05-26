# Backfill: lowercase the `query` on existing search Activity rows.
#
# Going forward, search.py and analytics.log_search_click store the query
# lowercased so "Vintage Cars" and "vintage cars" are one ranking signal
# (ACTIVITY_AND_FEED_AUDIT.md item A15). This migration normalizes the rows
# already in the table so a future search-history personalization feature
# (audit item B1) doesn't see a mix of cased and case-folded terms.
#
# Scope is deliberately narrow:
#   • Only `search_query` and `search_click` rows carry a meaningful query.
#   • SearchHistory rows are NOT touched — those are display rows and must
#     keep the user's original capitalization.
#
# Done as a single DB-side `UPDATE ... SET query = LOWER(query)` via the
# Lower() function expression, so it's one statement regardless of table
# size. (On SQLite, LOWER is ASCII-only; on PostgreSQL it's locale-aware.
# The live write path uses Python's full-Unicode str.lower(), so any
# residual non-ASCII rows self-correct the next time that term is searched.)
#
# Irreversible: lowercasing discards the original casing, so the reverse is
# a no-op rather than a (impossible) restore.

from django.db import migrations
from django.db.models.functions import Lower


def lowercase_search_queries(apps, schema_editor):
    Activity = apps.get_model("api", "Activity")
    (
        Activity.objects
        .filter(action_type__in=["search_query", "search_click"])
        .exclude(query="")
        .update(query=Lower("query"))
    )


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0073_backfill_post_hashtags'),
    ]

    operations = [
        migrations.RunPython(
            lowercase_search_queries,
            migrations.RunPython.noop,
        ),
    ]
