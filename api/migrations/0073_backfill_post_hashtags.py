# Backfill PostHashtag rows for every existing post that has hashtags in
# its description.
#
# Runs once. Reuses the SAME extraction logic the live code path uses
# (comment_analyzer.extract_hashtags) so backfilled tags are identical to
# tags that will be written for new posts — lowercased, de-duplicated,
# no leading '#'. See ACTIVITY_AND_FEED_AUDIT.md item A8.
#
# Batched with .iterator() + chunked bulk_create so the migration has a
# bounded memory footprint even on a multi-million-row Post table. We
# also cap tags-per-post to match utils._MAX_HASHTAGS_PER_POST so a post
# spamming hundreds of "#" tokens can't blow up the insert.

from django.db import migrations

# Inlined rather than imported from api.utils so the migration is pinned to
# the behaviour at the time it was written and won't drift if the constant
# is later retuned.
_MAX_HASHTAGS_PER_POST = 30
_BATCH = 5000


def backfill(apps, schema_editor):
    Post = apps.get_model("api", "Post")
    PostHashtag = apps.get_model("api", "PostHashtag")

    # Import the extractor lazily. It's pure-Python with no app-registry
    # dependency, so it's safe to call from inside a migration.
    from api.comment_analyzer import extract_hashtags

    # Only posts that actually contain a "#" can have hashtags — this
    # prefilter skips the overwhelming majority of rows cheaply.
    qs = (
        Post.objects
        .filter(description__contains="#")
        .exclude(description="")
        .values_list("id", "description")
        .iterator(chunk_size=_BATCH)
    )

    buffer = []
    for post_id, description in qs:
        tags = extract_hashtags(description or "")
        if not tags:
            continue
        if len(tags) > _MAX_HASHTAGS_PER_POST:
            tags = sorted(set(tags))[:_MAX_HASHTAGS_PER_POST]
        for tag in tags:
            buffer.append(PostHashtag(post_id=post_id, hashtag=tag[:100]))
        if len(buffer) >= _BATCH:
            PostHashtag.objects.bulk_create(buffer, ignore_conflicts=True)
            buffer = []

    if buffer:
        PostHashtag.objects.bulk_create(buffer, ignore_conflicts=True)


def unbackfill(apps, schema_editor):
    # Reverse simply empties the table — the forward migration is the only
    # thing that populated it at this point in history.
    PostHashtag = apps.get_model("api", "PostHashtag")
    PostHashtag.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0072_posthashtag'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
