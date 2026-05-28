"""The legacy followed / suggested feed builders. The newer four-rail
pipeline lives in api.feed; these are the older composers."""
from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.db.models import (
    Case, Count, Exists, IntegerField, OuterRef, Q, Subquery, Value, When,
)
from django.utils import timezone

from ...models import Comment, Follow, PageFollow, Post, PostLike, SavedPost

from .counts import likes_count_subquery, comments_count_subquery, saves_count_subquery
from .visibility import post_visibility_q
from .render import serialize_post, recency_decay

FEED_PAGE_SIZE = 10

# B9: how long a freshly-followed author's posts get priority in the followed
# feed, so a brand-new follow isn't drowned out by a steady year-old-follow
# poster. Posts from authors followed within this window sort ahead of the
# pure-recency stream.
FRESH_FOLLOW_WINDOW_HOURS = 24


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
