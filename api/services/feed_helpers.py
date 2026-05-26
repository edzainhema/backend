"""
Feed-building helpers used by the home, explore, and reels surfaces:
social-graph queries, recency decay, post serialisation, the rail-merge
heuristic, and the followed/suggested feed builders.

The newer four-rail home-feed pipeline lives in the `api.feed` package. That
package imports `get_followed_feed` from here (or, after the refactor,
indirectly via `api.views` which re-exports it).

Extracted from the monolithic views.py during the 2026-05 refactor.
"""
import math

from collections import defaultdict
from datetime import timedelta

from django.db.models import (
    Case, Count, Exists, F, IntegerField, OuterRef, Prefetch, Q, Subquery,
    Value, When,
)
from django.core.cache import cache
from django.db.models.functions import Coalesce
from django.utils import timezone

from ..models import (
    BlockedUser, Comment, Conversation, Follow, Message, MutedPage,
    MutedUser, NotInterested, Page, PageFollow, PagePoster, Post, PostLike,
    PostMedia, PostMediaTag, SavedPost,
)
from ..serializers import FeedPostSerializer
from ..post_media import ordered_media


# Page size for the legacy followed/suggested feed builders. The newer
# four-rail pipeline in api.feed uses its own PAGE_SIZE constant.
FEED_PAGE_SIZE = 10

# B9: how long a freshly-followed author's posts get priority in the followed
# feed, so a brand-new follow isn't drowned out by a steady year-old-follow
# poster. Posts from authors followed within this window sort ahead of the
# pure-recency stream.
FRESH_FOLLOW_WINDOW_HOURS = 24


# -----------------------------------------------------------------------------
# Engagement count subqueries (NQ-1)
# -----------------------------------------------------------------------------
# Stacking several Count(<rel>, distinct=True) for likes / comments / saves on
# ONE queryset makes the DB LEFT JOIN all three relations together and DISTINCT
# away an O(likes x comments x saves) cartesian product per post -- fine on a dev
# dataset, ruinous the moment a post goes viral. These build one correlated
# COUNT subquery per relation instead: each is an independent indexed count with
# no join multiplication. Coalesce(..., 0) keeps the "no rows" case at 0 (not
# NULL), matching the old Count() behaviour, and the serializers already read the
# *_ann / *_count values, so swapping the annotation expression is transparent.
#
# `outer` is the field on the OUTER queryset identifying the post: "pk" when the
# queryset is Post itself, or the post-FK name (e.g. "post_id") when annotating a
# model that points at Post (SavedPost, ...).

def _post_relation_count(model, outer):
    return Coalesce(
        Subquery(
            model.objects
            .filter(post=OuterRef(outer))
            .order_by()
            .values("post")
            .annotate(_c=Count("*"))
            .values("_c"),
            output_field=IntegerField(),
        ),
        0,
    )


def likes_count_subquery(outer="pk"):
    return _post_relation_count(PostLike, outer)


def comments_count_subquery(outer="pk"):
    return _post_relation_count(Comment, outer)


def saves_count_subquery(outer="pk"):
    return _post_relation_count(SavedPost, outer)


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


def recency_decay(created_at, half_life_hours=24):
    """
    Returns a multiplier between 0 and 1
    """
    age_hours = (
        timezone.now() - created_at
    ).total_seconds() / 3600

    return math.exp(-age_hours / half_life_hours)


def get_very_close_friend_ids(user, limit=15):
    """
    Returns a SET of user IDs ranked by relationship strength
    """

    now = timezone.now()
    since_30d = now - timedelta(days=30)

    scores = defaultdict(int)

    # --------------------------------------------------
    # 👯 Mutual follows (baseline)
    # --------------------------------------------------
    following = set(
        Follow.objects.filter(follower=user)
        .values_list("following_id", flat=True)
    )

    followers = set(
        Follow.objects.filter(following=user)
        .values_list("follower_id", flat=True)
    )

    mutuals = following & followers

    for uid in mutuals:
        scores[uid] += 10   # baseline weight

    # --------------------------------------------------
    # 💬 Recent DMs (very strong signal)
    # --------------------------------------------------
    messages = list(Message.objects.filter(
        Q(sender=user) | Q(conversation__participants=user),
        created_at__gte=since_30d
    ).select_related("sender"))

    # Batch-fetch the other participant for every conversation where the
    # current user is the sender.  Previously this hit the DB once per
    # message (N+1) and crashed with AttributeError when a conversation
    # had no other participants (.first() returned None).
    sender_conv_ids = {
        m.conversation_id for m in messages if m.sender_id == user.id
    }
    conv_other_map: dict = {}
    if sender_conv_ids:
        for conv_id, participant_id in (
            Conversation.objects
            .filter(id__in=sender_conv_ids)
            .values_list("id", "participants")
        ):
            # Keep only the first non-viewer participant per conversation.
            if participant_id != user.id and conv_id not in conv_other_map:
                conv_other_map[conv_id] = participant_id

    for m in messages:
        if m.sender_id != user.id:
            other = m.sender_id
        else:
            other = conv_other_map.get(m.conversation_id)
            if other is None:
                continue  # no other participant found — skip safely
        scores[other] += 5

    # --------------------------------------------------
    # 🏷️ Tagging in posts (strong signal)
    # --------------------------------------------------
    tags = PostMediaTag.objects.filter(
        media__post__user=user,
        created_at__gte=since_30d
    ).values_list("user_id", flat=True)

    for uid in tags:
        scores[uid] += 4

    # --------------------------------------------------
    # 💬 Commenting on each other
    # --------------------------------------------------
    comments = Comment.objects.filter(
        Q(user=user) | Q(post__user=user),
        created_at__gte=since_30d,
        is_deleted=False
    ).select_related("user", "post")

    for c in comments:
        other = c.user_id if c.user_id != user.id else c.post.user_id
        scores[other] += 2

    # --------------------------------------------------
    # ❤️ Likes on each other’s posts (weak)
    # --------------------------------------------------
    likes = PostLike.objects.filter(
        Q(user=user) | Q(post__user=user),
        created_at__gte=since_30d
    )

    for l in likes:
        other = l.user_id if l.user_id != user.id else l.post.user_id
        scores[other] += 1

    # --------------------------------------------------
    # 🔥 Sort + return top N
    # --------------------------------------------------
    ranked = sorted(
        scores.items(),
        key=lambda x: x[1],
        reverse=True
    )

    return {
        user_id
        for user_id, score in ranked[:limit]
        if score >= 10   # threshold
    }


def get_close_friend_ids(user):
    """
    Return the user's precomputed "very close friends" set (UB-1).

    Read path: the expensive 30-day relationship scan (get_very_close_friend_ids)
    moved to the nightly `build_close_friends` job, which writes each active
    user's set to UserCloseFriends. Here we read that row -- one indexed lookup,
    no scan. If there's no row yet (a brand-new user, or before the first
    nightly run) we fall back to computing it once and persisting it, mirroring
    how api.feed.affinity._build_activity_profile handles a missing profile. So
    an established user never triggers the scan on a request; build_feed_context's
    90s cache covers repeated reads within a window.

    An existing-but-empty row ([]) is authoritative (the user genuinely has no
    close friends) and does NOT trigger a recompute -- only a missing row does.
    """
    from ..models import UserCloseFriends

    row = (
        UserCloseFriends.objects
        .filter(user=user)
        .values_list("friend_ids", flat=True)
        .first()
    )
    if row is not None:
        return set(row)

    # No precomputed row yet: compute once, persist, return (bounded -- this
    # only fires before the user's first nightly build).
    ids = get_very_close_friend_ids(user)
    try:
        UserCloseFriends.objects.update_or_create(
            user=user, defaults={"friend_ids": sorted(ids)}
        )
    except Exception:   # persistence is best-effort, never break the feed
        pass
    return ids


def get_friend_ids(user):
    """
    Friends = mutual followers
    """
    following = Follow.objects.filter(
        follower=user
    ).values_list("following_id", flat=True)

    followers = Follow.objects.filter(
        following=user
    ).values_list("follower_id", flat=True)

    return set(following).intersection(set(followers))


def get_social_sets(user):
    """
    Returns all relevant social relationship ID sets for ranking.
    """

    following = set(
        Follow.objects.filter(
            follower=user
        ).values_list("following_id", flat=True)
    )

    followers = set(
        Follow.objects.filter(
            following=user
        ).values_list("follower_id", flat=True)
    )

    friends = following & followers  # mutuals

    following_only = following - friends
    followers_only = followers - friends

    return {
        "friends": friends,
        "following_only": following_only,
        "followers_only": followers_only,
    }


def get_social_overlap_score(viewer, author):
    """
    Measures how socially close `author` is to `viewer`
    """

    viewer_following = set(
        Follow.objects.filter(
            follower=viewer
        ).values_list("following_id", flat=True)
    )

    viewer_followers = set(
        Follow.objects.filter(
            following=viewer
        ).values_list("follower_id", flat=True)
    )

    author_following = set(
        Follow.objects.filter(
            follower=author
        ).values_list("following_id", flat=True)
    )

    author_followers = set(
        Follow.objects.filter(
            following=author
        ).values_list("follower_id", flat=True)
    )

    mutual_followers = len(viewer_followers & author_followers)
    mutual_following = len(viewer_following & author_following)

    return {
        "mutual_followers": mutual_followers,
        "mutual_following": mutual_following,
        "score": (mutual_followers * 3) + (mutual_following * 2)
    }


def _build_feed_context_uncached(user):
    """
    Compute all shared sets used across feed logic (no caching).
    Called only by build_feed_context — do not call directly.
    """

    # ---------------- BLOCKED USERS (both directions) ----------------
    blocked_pairs = BlockedUser.objects.involving(user).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_user_ids = set()
    for u, b in blocked_pairs:
        blocked_user_ids.add(u)
        blocked_user_ids.add(b)
    blocked_user_ids.discard(user.id)

    # Single query — reused for both "followed_users" and "viewer_following"
    # (they are the same set; previously two identical DB round-trips).
    followed_user_ids = set(
        Follow.objects.filter(
            follower=user
        ).values_list("following_id", flat=True)
    )

    viewer_followers = set(
        Follow.objects.filter(
            following=user
        ).values_list("follower_id", flat=True)
    )

    # ---------------- NOT-INTERESTED EXCLUSIONS ----------------
    # The viewer's explicit "show me less" choices (B2). One query, ordered
    # newest-first and bounded, then partitioned by kind. Author/topic sets
    # are naturally tiny; the cap only guards against a user who has dismissed
    # thousands of individual posts.
    not_interested_post_ids: set = set()
    not_interested_user_ids: set = set()
    not_interested_hashtags: set = set()
    for kind, pid, tuid, tag in (
        NotInterested.objects
        .filter(user=user)
        .order_by("-created_at")
        .values_list("kind", "post_id", "target_user_id", "hashtag")[:5000]
    ):
        if kind == NotInterested.KIND_POST and pid is not None:
            not_interested_post_ids.add(pid)
        elif kind == NotInterested.KIND_AUTHOR and tuid is not None:
            not_interested_user_ids.add(tuid)
        elif kind == NotInterested.KIND_TOPIC and tag:
            not_interested_hashtags.add(tag)

    return {
        "blocked_user_ids": blocked_user_ids,

        "not_interested_post_ids": not_interested_post_ids,
        "not_interested_user_ids": not_interested_user_ids,
        "not_interested_hashtags": not_interested_hashtags,

        "muted_user_ids": set(
            MutedUser.objects.filter(
                user=user
            ).values_list("muted_user_id", flat=True)
        ),

        "muted_page_ids": set(get_muted_page_ids(user)),

        "followed_users": followed_user_ids,

        "followed_pages": set(
            PageFollow.objects.filter(
                user=user
            ).values_list("page_id", flat=True)
        ),

        "very_close_friend_ids": get_friend_ids(user),

        # The genuine "very close friends" set — ranked by DMs, tags,
        # comments, and likes between the two people (get_very_close_friend_ids,
        # NOT plain mutuals). This was computed nowhere in the live ranking
        # path until now; the friend-network rail uses it to heavily weight an
        # author followed by the viewer's close friends (B8). Cached with the
        # rest of the context (90 s), so its handful of extra queries run at
        # most once per 90 s per viewer.
        "close_friend_ids": get_close_friend_ids(user),

        # Reuse the set computed above — no extra query.
        "viewer_following": followed_user_ids,

        "viewer_followers": viewer_followers,
    }


def serialize_post(
    post,
    user,
    request,
    *,
    suggested=False,
    top_comments=None
):
    # ---------------- USER AVATAR ----------------
    avatar = None
    profile = getattr(post.user, "userprofile", None)
    if profile and profile.avatar:
        avatar = request.build_absolute_uri(profile.avatar.url)

    # ---------------- PAGE AVATAR ----------------
    page_avatar = None
    if post.page and post.page.avatar:
        page_avatar = request.build_absolute_uri(post.page.avatar.url)

    return {
        "id": post.id,
        "description": post.description,
        "created_at": post.created_at,
        "suggested": suggested,
        "is_public_override": post.is_public_override,

        "user": {
            "id": post.user.id,
            "username": post.user.username,
            "avatar": avatar,
        },

        "page": (
            {
                "id": post.page.id,
                "name": post.page.name,
                "avatar": page_avatar,
                "is_private": post.page.is_private,
            }
            if post.page else None
        ),

        # Prefer annotation values (set by get_followed_feed / get_suggested_feed)
        # to avoid N+1 queries. Fall back to live queries only when the post
        # object comes from a code path that doesn't annotate (e.g. tests).
        "likes_count": getattr(post, "likes_count_ann", None) if getattr(post, "likes_count_ann", None) is not None else post.likes.count(),
        "is_liked": bool(getattr(post, "viewer_liked", None)) if getattr(post, "viewer_liked", None) is not None else post.likes.filter(user=user).exists(),
        "is_owner": post.user_id == user.id,

        "comments_count": getattr(post, "comments_count_ann", None) if getattr(post, "comments_count_ann", None) is not None else post.comments.count(),

        "saves_count": getattr(post, "saves_count_ann", None) if getattr(post, "saves_count_ann", None) is not None else post.saved_by.count(),
        "is_saved": bool(getattr(post, "viewer_saved", None)) if getattr(post, "viewer_saved", None) is not None else post.saved_by.filter(user=user).exists(),

        "is_followed": bool(getattr(post, "viewer_follows_author", None)) if getattr(post, "viewer_follows_author", None) is not None else Follow.objects.filter(follower=user, following=post.user).exists(),
        "is_page_followed": (
            bool(getattr(post, "viewer_follows_page", None)) if getattr(post, "viewer_follows_page", None) is not None
            else PageFollow.objects.filter(user=user, page=post.page).exists()
            if post.page else False
        ),

        "top_comments": top_comments or [],

        # Read media from the `ordered_media` attr (set via Prefetch with
        # to_attr=...) when available; otherwise sort the prefetched cache in
        # Python. Calling `post.media.all().order_by("order")` directly would
        # bust `prefetch_related("media")` and re-query per post — see the
        # same fix in serializers.py FeedPostSerializer.get_media.
        "media": [
            {
                "id": m.id,
                "file": request.build_absolute_uri(m.file.url),
                "thumbnail": (
                    request.build_absolute_uri(m.thumbnail.url)
                    if m.thumbnail else None
                ),
                "order": m.order,
                # Pixel dimensions captured at upload time. Null for legacy
                # rows uploaded before the column existed — the client
                # falls back to Image.getSize / video naturalSize then.
                "width": m.width,
                "height": m.height,
                "tags": [
                    {
                        "id": t.user.id,
                        "username": t.user.username,
                    }
                    for t in m.tags.all()
                ],
            }
            for m in ordered_media(post)
        ],
    }


def merge_feed(primary, secondary, interval=5):
    merged = []
    s_idx = 0

    for i, item in enumerate(primary):
        merged.append(item)

        if i % interval == interval - 1 and s_idx < len(secondary):
            merged.append(secondary[s_idx])
            s_idx += 1

    # When the followed feed is short or empty, append remaining suggested
    # posts so users with small follow graphs still see a full feed.
    while s_idx < len(secondary):
        merged.append(secondary[s_idx])
        s_idx += 1

    return merged


def build_feed_context(user):
    """
    Return the per-user sets needed by feed queries.
    Results are cached per user for 90 s and invalidated on
    block / mute / follow actions so changes take effect quickly.
    """
    cache_key = f"feed_ctx:{user.id}"
    ctx = cache.get(cache_key)
    if ctx is None:
        ctx = _build_feed_context_uncached(user)
        cache.set(cache_key, ctx, timeout=90)
    return ctx


def get_followed_feed(request, user, context, before=None, before_id=None):
    # ------------------------------------------------------------------
    # Feed visibility rules
    # ------------------------------------------------------------------
    # (a) Posts from followed users that are posted in:
    #     (a-i)   an open (public) page
    #     (a-ii)  a private page the viewer also follows
    #     (a-iii) a private or super-private page the viewer does NOT
    #             follow, but the poster set is_public_override=True.
    #             Both flags are checked independently because the model
    #             allows them to be set separately.
    #
    # (b) All posts from any page the viewer directly follows, regardless
    #     of who posted them (this is a superset of a-ii).
    #
    # Personal posts (no page) from followed users are also included.
    # ------------------------------------------------------------------
    qs = (
        Post.objects
        .filter(
            # (a-i) Followed user posted in an open/public page
            Q(user_id__in=context["followed_users"], page__is_private=False) |

            # (a-ii) Followed user posted in a private page the viewer also follows
            Q(
                user_id__in=context["followed_users"],
                page__is_private=True,
                page_id__in=context["followed_pages"],
            ) |

            # (a-iii) Followed user posted in a private page the viewer does NOT
            #         follow, but the poster made it visible via public override
            Q(
                user_id__in=context["followed_users"],
                page__is_private=True,
                is_public_override=True,
            ) |

            # (a-iv) Same as (a-iii) but for super-private pages — checked
            #        separately because is_super_private and is_private are
            #        independent boolean fields on the Page model.
            Q(
                user_id__in=context["followed_users"],
                page__is_super_private=True,
                is_public_override=True,
            ) |

            # (b) All posts from any page the viewer follows, regardless of poster
            Q(page_id__in=context["followed_pages"]) |

            # (c) The viewer's own posts
            Q(user_id=user.id)
        )
        .distinct()
        .exclude(user_id__in=context["blocked_user_ids"])
        .exclude(user_id__in=context["muted_user_ids"])
        .exclude(page_id__in=context["muted_page_ids"])
        .select_related("user", "user__userprofile", "page")
        .prefetch_related(
            # Only `media` (+ tags) is read off these post objects, by
            # serialize_post. Comments are deliberately NOT prefetched here:
            # the count comes from the comments_count_ann annotation below, and
            # the top comments come from one batched query (all_top_comments)
            # further down. Prefetching comments__user / __userprofile /
            # __likes used to load every comment — and every comment-like — of
            # every feed post into memory, then discard all of it: a large,
            # pure waste on posts with busy threads, paid on every /feed/ hit.
            "media", "media__tags", "media__tags__user",
        )
        # Annotate all per-user counts/flags in a single query — eliminates
        # 6-7 extra DB hits per post that serialize_post used to fire.
        .annotate(
            likes_count_ann=likes_count_subquery(),
            comments_count_ann=comments_count_subquery(),
            saves_count_ann=saves_count_subquery(),
            viewer_liked=Exists(
                PostLike.objects.filter(post=OuterRef("pk"), user=user)
            ),
            viewer_saved=Exists(
                SavedPost.objects.filter(post=OuterRef("pk"), user=user)
            ),
            viewer_follows_author=Exists(
                Follow.objects.filter(follower=user, following=OuterRef("user"))
            ),
            viewer_follows_page=Exists(
                PageFollow.objects.filter(user=user, page=OuterRef("page"))
            ),
            # B9: when the viewer followed this post's author (NULL for the
            # viewer's own posts / page-only follows).
            follow_created_at=Subquery(
                Follow.objects
                .filter(follower=user, following=OuterRef("user"))
                .values("created_at")[:1]
            ),
        )
        # B9: flag posts whose author was followed within the fresh window, and
        # sort those ahead of the pure-recency stream so a brand-new follow's
        # content surfaces instead of being buried under a prolific old follow.
        .annotate(
            fresh_follow_ann=Case(
                When(
                    follow_created_at__gte=(
                        timezone.now() - timedelta(hours=FRESH_FOLLOW_WINDOW_HOURS)
                    ),
                    then=Value(1),
                ),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        .order_by("-fresh_follow_ann", "-created_at")
    )

    # Cursor filter — compound (created_at, id) prevents skipping/duplicating
    # posts when two posts share the same created_at timestamp.
    if before:
        if before_id is not None:
            from django.db.models import Q as _Q
            qs = qs.filter(
                _Q(created_at__lt=before) |
                _Q(created_at=before, id__lt=before_id)
            )
        else:
            qs = qs.filter(created_at__lt=before)

    # We only need enough to fill one page of the merged feed.
    # Worst case: every slot is a followed post, so PAGE_SIZE is enough.
    post_list = list(qs[:FEED_PAGE_SIZE * 2])

    # ------------------------------------------------------------------
    # Batch-fetch top comments for all posts in one query — eliminates
    # the N+1 that fired a separate annotated sub-query per post.
    # ------------------------------------------------------------------
    post_ids = [p.id for p in post_list]
    friend_ids = context["very_close_friend_ids"]

    all_top_comments = (
        Comment.objects
        .filter(
            post_id__in=post_ids,
            parent__isnull=True,
            is_deleted=False,
            user_id__in=friend_ids,
        )
        .select_related("user", "user__userprofile")
        .annotate(like_count=Count("likes"))
        .order_by("post_id", "-like_count", "-created_at")
    )

    # Group by post_id, keep top 5 per post.
    comments_by_post: dict = {}
    for c in all_top_comments:
        bucket = comments_by_post.setdefault(c.post_id, [])
        if len(bucket) < 5:
            bucket.append(c)

    feed = []
    for post in post_list:
        raw_comments = comments_by_post.get(post.id, [])
        top_comments_data = []
        for c in raw_comments:
            up = getattr(c.user, "userprofile", None)
            top_comments_data.append({
                "id": c.id,
                "text": c.text,
                "user": {
                    "id": c.user.id,
                    "username": c.user.username,
                    "avatar": (
                        request.build_absolute_uri(up.avatar.url)
                        if up and up.avatar else None
                    ),
                },
            })

        serialized_post = serialize_post(
            post=post, user=user, request=request,
            suggested=False, top_comments=top_comments_data,
        )
        # B9: expose the fresh-follow flag so the home-feed cursor can stay
        # chronological (boosted posts must not drag the cursor backwards).
        serialized_post["is_fresh_follow"] = bool(
            getattr(post, "fresh_follow_ann", 0)
        )
        feed.append(serialized_post)

    return feed


def get_suggested_feed(request, user, context, limit=20, offset=0):
    # Cache scored post IDs per user to avoid re-scoring on every page load.
    # Invalidate after 5 minutes so the ranking stays reasonably fresh.
    cache_key = f"suggested_feed_scores:{user.id}"
    scored_ids = cache.get(cache_key)

    if scored_ids is None:
        # Cap candidates to avoid unbounded queries on large datasets
        MAX_CANDIDATES = 500
        candidates = (
            Post.objects
            .filter(post_visibility_q(
                user, context["followed_users"], context["followed_pages"],
            ))
            .exclude(user_id__in=context["followed_users"])
            .exclude(user_id=user.id)
            .exclude(user_id__in=context["blocked_user_ids"])
            .exclude(user_id__in=context["muted_user_ids"])
            .exclude(page_id__in=context["muted_page_ids"])
            .distinct()
            .select_related("user", "user__userprofile", "page")
            .prefetch_related(
                "media", "media__tags", "media__tags__user",
            )
            .annotate(
                # Rename to _ann so serialize_post can detect these safely
                likes_count_ann=likes_count_subquery(),
                comments_count_ann=comments_count_subquery(),
                saves_count_ann=saves_count_subquery(),
                viewer_liked=Exists(
                    PostLike.objects.filter(post=OuterRef("pk"), user=user)
                ),
                viewer_saved=Exists(
                    SavedPost.objects.filter(post=OuterRef("pk"), user=user)
                ),
                viewer_follows_author=Exists(
                    Follow.objects.filter(follower=user, following=OuterRef("user"))
                ),
                viewer_follows_page=Exists(
                    PageFollow.objects.filter(user=user, page=OuterRef("page"))
                ),
            )
            .order_by("-created_at")[:MAX_CANDIDATES]
        )

        # Batch-fetch the entire follow graph for all candidate authors in
        # two queries instead of 2-per-post (previously O(N) queries).
        candidate_user_ids = list({p.user_id for p in candidates})

        # author_id → set of their follower IDs
        author_followers_map: dict[int, set] = defaultdict(set)
        for follower_id, following_id in (
            Follow.objects
            .filter(following_id__in=candidate_user_ids)
            .values_list("follower_id", "following_id")
        ):
            author_followers_map[following_id].add(follower_id)

        # author_id → set of IDs they follow
        author_following_map: dict[int, set] = defaultdict(set)
        for follower_id, following_id in (
            Follow.objects
            .filter(follower_id__in=candidate_user_ids)
            .values_list("follower_id", "following_id")
        ):
            author_following_map[follower_id].add(following_id)

        scored = []
        for post in candidates:
            author_followers = author_followers_map[post.user_id]
            author_following = author_following_map[post.user_id]

            mutual_followers = len(context["viewer_followers"] & author_followers)
            mutual_following = len(context["viewer_following"] & author_following)

            social_score = mutual_followers * 3 + mutual_following * 2
            engagement_score = (
                post.likes_count_ann * 2 +
                post.comments_count_ann * 3 +
                post.saves_count_ann * 4
            )
            score = (social_score + engagement_score) * recency_decay(post.created_at)

            if score >= 5:
                scored.append((post.id, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        scored_ids = scored
        cache.set(cache_key, scored_ids, timeout=300)

        # Return serialized posts from the already-fetched candidate objects.
        # Build an id→post map so we can respect the sorted order.
        post_map = {p.id: p for p in candidates}
        return [
            serialize_post(post=post_map[pid], user=user, request=request, suggested=True)
            for pid, _ in scored_ids[offset: offset + limit]
            if pid in post_map
        ]

    # Cache hit — re-fetch only the posts we actually need for this page slice.
    page_ids = [pid for pid, _ in scored_ids[offset: offset + limit]]
    if not page_ids:
        return []

    posts_qs = (
        Post.objects
        .filter(id__in=page_ids)
        .select_related("user", "user__userprofile", "page")
        .prefetch_related("media", "media__tags", "media__tags__user")
        .annotate(
            likes_count_ann=likes_count_subquery(),
            comments_count_ann=comments_count_subquery(),
            saves_count_ann=saves_count_subquery(),
            viewer_liked=Exists(
                PostLike.objects.filter(post=OuterRef("pk"), user=user)
            ),
            viewer_saved=Exists(
                SavedPost.objects.filter(post=OuterRef("pk"), user=user)
            ),
            viewer_follows_author=Exists(
                Follow.objects.filter(follower=user, following=OuterRef("user"))
            ),
            viewer_follows_page=Exists(
                PageFollow.objects.filter(user=user, page=OuterRef("page"))
            ),
        )
    )
    post_map = {p.id: p for p in posts_qs}

    return [
        serialize_post(post=post_map[pid], user=user, request=request, suggested=True)
        for pid in page_ids
        if pid in post_map
    ]
