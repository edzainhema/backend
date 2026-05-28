"""Post hashtag sync: reconcile a post's #tags into PostHashtag rows."""
import logging

logger = logging.getLogger(__name__)


_MAX_HASHTAGS_PER_POST = 30


def sync_post_hashtags(post):
    """
    Reconcile a post's PostHashtag rows with the hashtags currently in its
    description. Idempotent and diff-based: safe to call on create and on
    any future edit path — it only inserts genuinely-new tags and deletes
    tags that are no longer present.

    Best-effort: never raises into the request path. Hashtag indexing is
    a ranking optimisation, not a correctness requirement, so a failure
    here must not break post creation. (Mirrors log_activity's contract.)

    Returns the set of tags now stored for the post, or None on failure.
    """
    from ..models import PostHashtag
    from .comment_analyzer import extract_hashtags

    try:
        # extract_hashtags returns lowercased, de-duplicated tags without
        # the leading '#'. Clamp each to the column width and cap the count.
        wanted = {
            t[:100] for t in extract_hashtags(post.description or "")
        }
        if len(wanted) > _MAX_HASHTAGS_PER_POST:
            # Deterministic truncation: sort so the same post always keeps
            # the same subset rather than relying on set iteration order.
            wanted = set(sorted(wanted)[:_MAX_HASHTAGS_PER_POST])

        existing = set(
            PostHashtag.objects
            .filter(post=post)
            .values_list("hashtag", flat=True)
        )

        to_add = wanted - existing
        to_remove = existing - wanted

        if to_remove:
            PostHashtag.objects.filter(
                post=post, hashtag__in=to_remove
            ).delete()

        if to_add:
            PostHashtag.objects.bulk_create(
                [PostHashtag(post=post, hashtag=t) for t in to_add],
                ignore_conflicts=True,
            )

        return wanted
    except Exception as exc:
        post_id = getattr(post, "id", None)
        logger.error(f"[sync_post_hashtags] failed for post {post_id}: {exc}")
        return None
