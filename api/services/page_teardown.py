"""Tearing down a page: cascade its deletion down to the posts on it.

This is the single shared path for "a page is going away" — used both when an
admin trashes their page and (later, Phase 6) when an admin's account is
deleted. It exists because ``Post.page`` is ``SET_NULL``: a *raw* page delete
would orphan the page's live posts to ``page_id = NULL`` (a page-less live post,
which the app forbids). So every page-removal must first move the page's posts
into their authors' trash.
"""
from django.contrib.auth.models import User
from django.utils import timezone

from ..models import Notification, Post
from .post_cleanup import purge_post_files
from .push import push_to_user


def teardown_page(page, *, actor, notify=True):
    """Move every still-live post on ``page`` into its author's trash (reason
    ``"page_deleted"``, keeping ``page_id`` as the origin pointer) and notify
    each distinct contributor — except ``actor`` (the person doing the
    deletion, who already knows). Returns the number of posts trashed.

    Idempotent-ish: posts already trashed are skipped, so re-running on a page
    that's been partially torn down won't double-notify for those.

    ``notify=False`` skips the notifications — used by account deletion, where
    the actor (the departing user) is about to be deleted, so any notification
    naming them as the actor would just cascade away with them.
    """
    now = timezone.now()

    # all_objects: the default manager hides trashed posts, but we only want the
    # still-live ones here anyway (trashed_at is null). only() keeps it cheap.
    live = list(
        Post.all_objects
        .filter(page=page, trashed_at__isnull=True)
        .only("id", "user_id")
    )
    if not live:
        return 0

    post_ids = [p.id for p in live]
    Post.all_objects.filter(id__in=post_ids).update(
        trashed_at=now,
        trashed_reason="page_deleted",
    )

    # Notify each distinct author except the actor. Create individually (not a
    # bulk_create) so the post_save signal fires per row — that's what keeps the
    # recipient's unread-badge cache correct (see services/notification_cache).
    author_ids = {p.user_id for p in live if p.user_id != actor.id}
    if notify and author_ids:
        for author in User.objects.filter(id__in=author_ids):
            Notification.objects.create(
                recipient=author,
                actor=actor,
                notification_type="page_deleted",
                page=page,
            )
            push_to_user(
                author,
                title="A page was deleted",
                body=f"“{page.name}” was deleted. Your posts from it are in your Trash.",
                actor=actor,
                page=page,
            )

    return len(post_ids)


def notify_page_restored(page, *, actor):
    """After a page is restored from trash, notify each contributor (except
    ``actor``) who still has posts from it sitting in their trash — the ones
    trashed by the original page deletion. The notification is actionable: the
    contributor can restore those posts back into the now-live page or keep
    them. Returns the number of contributors notified."""
    contributor_ids = set(
        Post.all_objects
        .filter(page=page, trashed_at__isnull=False, trashed_reason="page_deleted")
        .exclude(user_id=actor.id)
        .values_list("user_id", flat=True)
        .distinct()
    )
    if not contributor_ids:
        return 0

    for contributor in User.objects.filter(id__in=contributor_ids):
        Notification.objects.create(
            recipient=contributor,
            actor=actor,
            notification_type="page_restored",
            page=page,
        )
        push_to_user(
            contributor,
            title="A page was restored",
            body=f"“{page.name}” is back. Restore your posts into it?",
            actor=actor,
            page=page,
        )

    return len(contributor_ids)


def teardown_user_owned_pages(user):
    """Run before a user is deleted: trash the posts on every LIVE page they own
    so other contributors' posts don't orphan into a page-less *live* state when
    the page row is cascade-deleted with the user (``Post.page`` is SET_NULL).
    No notifications — the actor is leaving, so notification rows naming them
    would just cascade away too.

    Also best-effort-deletes the departing user's OWN post media from storage
    (every one of their posts, trashed or not, is about to be cascade-deleted
    with them, leaving the files orphaned otherwise). Wired to User's pre_delete
    signal in apps.py, so it fires however the user is removed (admin, a future
    delete-account endpoint, a data script).
    """
    from ..models import Page

    for page in Page.all_objects.filter(owner=user, deleted_at__isnull=True):
        teardown_page(page, actor=user, notify=False)

    for post in Post.all_objects.filter(user=user):
        purge_post_files(post)
