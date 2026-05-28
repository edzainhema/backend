"""Post engagement: like, save, and not-interested, plus activity-surface and feed-cache helpers."""
import logging


from django.core.cache import cache
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Activity, NotInterested, Notification, Post, PostHashtag, PostLike, SavedPost
from ...services.feed_helpers import viewer_can_see_post
from ...services.activity import log_activity
from ...services.push import push_to_user

logger = logging.getLogger(__name__)

def _activity_surface(request, default=""):
    """
    Resolve and validate the client-supplied `surface` for an Activity row.

    Likes and saves now carry the surface they happened on (home / reels /
    profile / post_detail / …) so the feed ranker can tell apart a user who
    only likes reels from one who only likes home-feed posts — see
    ACTIVITY_AND_FEED_AUDIT.md item A12.

    We validate against Activity.SURFACE_CHOICES so a buggy or hostile
    client can't write arbitrary strings into the analytics stream. Unknown
    or missing values fall back to `default` (empty string = "unknown"),
    which both matches the model's field default and preserves the previous
    behaviour for older app builds that don't send the field yet — they
    simply log an empty surface exactly as they did before, while updated
    clients enrich it.
    """
    valid = {code for code, _ in Activity.SURFACE_CHOICES}
    surface = request.data.get("surface")
    return surface if surface in valid else default



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_post_like(request):
    post_id = request.data.get("post_id")

    if not post_id:
        return Response(
            {"error": "post_id required"},
            status=400
        )

    post = get_object_or_404(
        Post.objects.select_related("user", "user__userprofile", "page"),
        id=post_id,
    )
    post_owner = post.user
    surface = _activity_surface(request)

    # --------------------------------------------------
    # 👁️  VISIBILITY GATE (LIKER ↔ POST)
    # Same rule the feed / reels / comments use. It already covers a block in
    # either direction with the post author, so this subsumes the old block
    # check. Return 404 (existence-hiding) so a like can't be used to probe
    # whether a post the viewer can't otherwise see (private account / page
    # they don't follow) exists.
    # --------------------------------------------------
    if not viewer_can_see_post(request.user, post):
        return Response({"error": "Not found"}, status=404)

    # --------------------------------------------------
    # ❤️ TOGGLE LIKE
    # --------------------------------------------------
    like, created = PostLike.objects.get_or_create(
        user=request.user,
        post=post
    )

    # --------------------------------------------------
    # ❌ UNLIKE
    # --------------------------------------------------
    if not created:
        like.delete()
        # Remove the matching "X liked your post" notification so the
        # recipient's feed doesn't keep showing a like that no longer
        # exists. Mirrors the cleanup the comment-like endpoint already
        # does. Self-likes (no notification was created) are a no-op.
        Notification.objects.filter(
            recipient=post.user,
            actor=request.user,
            notification_type="like",
            media=post,
        ).delete()
        log_activity(request.user, "post_unlike", post=post, surface=surface)
        return Response({"liked": False})

    # 📊 ACTIVITY
    log_activity(request.user, "post_like", post=post, surface=surface)

    # --------------------------------------------------
    # 🔔 NOTIFICATION (ONLY IF ALLOWED)
    # --------------------------------------------------
    if post_owner != request.user:
        Notification.objects.create(
            recipient=post_owner,
            actor=request.user,
            notification_type="like",
            media=post,
        )

        push_to_user(
            post_owner,
            title="New like",
            body=f"{request.user.username} liked your post",
            extra_data={
                "type": "post_like",
                "post_id": post.id,
                "actor_id": request.user.id,
            },
        )

    return Response({"liked": True})



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_post_save(request):
    post_id = request.data.get("post_id")

    if not post_id:
        return Response(
            {"error": "post_id required"},
            status=400
        )

    post = get_object_or_404(
        Post.objects.select_related("user", "user__userprofile", "page"),
        id=post_id,
    )
    post_owner = post.user
    surface = _activity_surface(request)

    # --------------------------------------------------
    # 👁️  VISIBILITY GATE (SAVER ↔ POST)
    # Same rule as toggle_post_like — visibility check that also covers blocks
    # in both directions. 404 keeps a save from confirming a hidden post.
    # --------------------------------------------------
    if not viewer_can_see_post(request.user, post):
        return Response({"error": "Not found"}, status=404)

    # --------------------------------------------------
    # 🔖 TOGGLE SAVE
    # --------------------------------------------------
    saved, created = SavedPost.objects.get_or_create(
        user=request.user,
        post=post
    )

    # --------------------------------------------------
    # ❌ UNSAVE
    # --------------------------------------------------
    if not created:
        saved.delete()
        log_activity(request.user, "post_unsave", post=post, surface=surface)
        return Response({"saved": False})

    log_activity(request.user, "post_save", post=post, surface=surface)
    return Response({"saved": True})



def _invalidate_feed_caches(user_id):
    """
    Drop the per-user feed caches so a just-recorded 'not interested' takes
    effect on the very next feed load instead of waiting out a rail's TTL.

    Covers the per-user caches: the shared feed context (which holds the
    exclusion sets), the activity profile (so the negative affinity is
    rebuilt), and the activity / friend-network scored lists. The nearby
    cache is geohash-suffixed and trending is global, so those aren't
    invalidated here — they're single-slot rails and self-correct within
    their short TTLs once the candidate query re-runs with the new
    exclusions.
    """
    try:
        cache.delete_many([
            f"feed_ctx:{user_id}",
            f"feed:activity_profile:{user_id}",
            f"feed:activity_scores:{user_id}",
            f"feed:friend_network:{user_id}",
            f"suggested_feed_scores:{user_id}",
        ])
    except Exception as exc:
        logger.error(f"[_invalidate_feed_caches] failed for {user_id}: {exc}")



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def not_interested(request):
    """
    Record an explicit "show me less of this" from the post long-press menu.

    Body:
      • post_id (required) — the post the action was taken on.
      • kind    (required) — "post" | "author" | "topic".
      • hashtag (optional, topic only) — a specific tag to mute. When omitted
        for kind="topic", every hashtag on the post (capped) is muted.

    Effects (see ACTIVITY_AND_FEED_AUDIT.md item B2):
      • post   → hide just this post from the viewer's discovery rails.
      • author → exclude the author from discovery + a strong negative
                 affinity signal (a `not_interested` Activity row).
      • topic  → exclude the hashtag(s) from discovery + negative affinity.

    Best-effort and idempotent: re-marking the same target is a no-op thanks
    to the model's partial unique constraints.
    """
    post_id = request.data.get("post_id")
    kind = request.data.get("kind")

    if not post_id or kind not in (
        NotInterested.KIND_POST,
        NotInterested.KIND_AUTHOR,
        NotInterested.KIND_TOPIC,
    ):
        return Response({"error": "post_id and valid kind required"}, status=400)

    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return Response({"error": "invalid post_id"}, status=400)

    post = get_object_or_404(Post, id=post_id)
    user = request.user

    if kind == NotInterested.KIND_POST:
        NotInterested.objects.get_or_create(
            user=user, kind=kind, post=post,
        )
        # Pure hide — no affinity penalty for a single disliked post.

    elif kind == NotInterested.KIND_AUTHOR:
        if post.user_id == user.id:
            return Response({"error": "cannot mute yourself"}, status=400)
        NotInterested.objects.get_or_create(
            user=user, kind=kind, target_user_id=post.user_id,
        )
        # Strong negative affinity toward this author.
        log_activity(user, "not_interested", target_user_id=post.user_id)

    else:  # KIND_TOPIC
        raw = request.data.get("hashtag")
        if isinstance(raw, str) and raw.strip():
            tags = [raw.strip().lstrip("#").strip().lower()[:100]]
        else:
            # No specific tag given — mute every hashtag on the post (capped).
            tags = list(
                PostHashtag.objects
                .filter(post=post)
                .values_list("hashtag", flat=True)[:10]
            )
        tags = [t for t in dict.fromkeys(tags) if t]   # dedupe, drop empties
        if not tags:
            return Response({"error": "no topic on this post"}, status=400)
        for tag in tags:
            NotInterested.objects.get_or_create(
                user=user, kind=kind, hashtag=tag,
            )
            log_activity(user, "not_interested", hashtag=tag)

    _invalidate_feed_caches(user.id)
    return Response({"status": "ok"})
