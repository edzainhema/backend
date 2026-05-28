"""Who-can-see-what: page-poster permission, muted pages, the queryset-level
post visibility filter (`post_visibility_q`), and the single-post check
(`viewer_can_see_post`)."""
from django.db.models import Q

from ...models import (
    BlockedUser, Follow, MutedPage, PageFollow, PagePoster,
)


def can_user_post_on_page(user, page):
    if page.owner == user:
        return True

    if page.anyone_can_post:
        return True

    return PagePoster.objects.filter(
        page=page,
        user=user
    ).exists()


def get_muted_page_ids(user):
    """
    Returns a queryset of page IDs muted by the user.
    Reusable across feeds, reels, search, etc.
    """
    return MutedPage.objects.filter(
        user=user
    ).values_list("page_id", flat=True)


def post_visibility_q(user, followed_user_ids, followed_page_ids):
    """
    Returns a Q object that, when chained onto a Post queryset via
    ``.filter(post_visibility_q(...))``, keeps only posts the given user is
    allowed to see.

    This is the single source of truth for "can this viewer see this post"
    used by every surface that returns posts the viewer didn't necessarily
    author: explore, reels, search, suggested, and the four api.feed
    rails. The home feed (``get_followed_feed``) inlines an equivalent rule
    because it also needs to *include* posts by relationship (followed
    users / followed pages), not just *exclude* private ones.

    The rule mirrors ``viewer_can_see_post`` (the single-post form, defined
    just below):

      * The viewer's own posts are always visible.
      * Personal posts (no page) are visible unless the author has a
        private profile and the viewer doesn't follow them.
      * Public-page posts (neither is_private nor is_super_private) are
        visible to anyone.
      * Private or super-private page posts are visible only to viewers
        who follow the page, OR when the author opted into
        ``is_public_override`` AND the viewer follows the author.

    Callers should still ``.distinct()`` the resulting queryset because
    the underlying joins can multiply rows. ``followed_user_ids`` and
    ``followed_page_ids`` may be a set, list, or values_list — anything
    accepted by ``__in``.
    """
    # The viewer's own posts — bypass every other gate.
    own = Q(user_id=user.id)

    # Personal (no page) post.
    #
    # Author is acceptable if:
    #   - they're followed, OR
    #   - their profile is not private, OR
    #   - they have no UserProfile row (defensive: matches _viewer_can_see_post,
    #     which uses getattr(..., None) and treats missing profile as public).
    no_page_visible = (
        Q(page__isnull=True) & (
            Q(user_id__in=followed_user_ids)
            | Q(user__userprofile__is_private=False)
            | Q(user__userprofile__isnull=True)
        )
    )

    # Fully public page post.
    public_page = (
        Q(page__isnull=False)
        & Q(page__is_private=False)
        & Q(page__is_super_private=False)
    )

    # Private / super-private page post: viewer follows the page, OR the
    # author flagged the individual post public AND the viewer follows
    # the author.
    private_page_visible = (
        Q(page__isnull=False)
        & (Q(page__is_private=True) | Q(page__is_super_private=True))
        & (
            Q(page_id__in=followed_page_ids)
            | (Q(is_public_override=True) & Q(user_id__in=followed_user_ids))
        )
    )

    return own | no_page_visible | public_page | private_page_visible


def viewer_can_see_post(viewer, post):
    """
    Single-post counterpart to ``post_visibility_q``: returns True iff
    ``viewer`` is allowed to see ``post``.

    This is the canonical gate for any endpoint that lets a viewer *interact
    with* a post they reached by id — commenting, replying, liking, saving,
    or liking a comment. Sharing one implementation with ``post_visibility_q``
    (the queryset form used by feed/reels/explore/search) means those write
    endpoints can't be used to act on — or enumerate the existence of —
    content the viewer could never see in the first place.

    The rules mirror ``post_visibility_q`` / ``get_followed_feed`` exactly:

      - the viewer's own posts are always visible;
      - a block in EITHER direction hides the post;
      - when the post is attached to a page, the *page's* visibility rules
        govern access; the author's profile-level privacy does NOT apply,
        because the post was published in a page context the viewer reached
        via the page (not via the author's profile):
          * a public page's posts are visible to everyone;
          * a private / super-private page's posts are visible to anyone who
            follows the page, OR — when the page isn't followed — to viewers
            who follow the author of a post flagged ``is_public_override``;
      - when the post is NOT attached to a page (a personal/profile post), a
        private-account author requires the viewer to follow them.

    Pass a `post` with `user`, `user__userprofile`, and `page` available
    (select_related) when calling in a hot path to avoid extra queries.
    """
    if post.user_id == viewer.id:
        return True

    if BlockedUser.objects.between(viewer, post.user_id).exists():
        return False

    # Page-attached post: page rules win, author privacy does not apply.
    # This matches get_followed_feed, where condition (b)
    # `Q(page_id__in=followed_pages)` includes every post on a followed
    # page regardless of the poster's profile privacy, and condition
    # (a-i) `page__is_private=False` surfaces every post in an open page
    # without consulting the author's privacy flag at all. The reels
    # query (views/reels.py) is the same — page-privacy only, no
    # author-privacy filter.
    page = post.page
    if page is not None:
        if page.is_private or page.is_super_private:
            if PageFollow.objects.filter(
                user=viewer, page_id=page.id
            ).exists():
                return True
            # Private page the viewer doesn't follow: the only remaining
            # path is the author's explicit public-override + viewer
            # following the author.
            if not post.is_public_override:
                return False
            return Follow.objects.filter(
                follower=viewer, following_id=post.user_id
            ).exists()
        # Public page: anyone who can reach the page can see its posts
        # (and therefore interact with them), regardless of the author's
        # profile-level privacy. A private-account user who posts in a
        # public page has, by that act, made that one post publicly
        # visible — the feed and reels treat it that way, and so must the
        # write endpoints.
        return True

    # No page attached — this is a personal/profile post. The author's
    # profile-level privacy is the only gate.
    author_profile = getattr(post.user, "userprofile", None)
    if author_profile and author_profile.is_private:
        return Follow.objects.filter(
            follower=viewer, following_id=post.user_id
        ).exists()

    return True


