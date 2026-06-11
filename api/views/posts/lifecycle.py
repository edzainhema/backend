"""Post lifecycle: delete and public/visibility toggle."""


from django.core.cache import cache
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Follow, Page, Post
from ...services.feed_helpers.visibility import can_user_post_on_page
from ...services.post_cleanup import purge_post_files

VIDEO_EXTS = (".mp4", ".mov", ".webm")

# Cap on a single bulk-trash request so a pathological client can't send a
# giant IN (...) clause. Far above any realistic multi-select.
MAX_BULK_TRASH = 500


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delete_post(request):
    """Move the author's own post(s) to trash (soft delete).

    Accepts EITHER a single ``post_id`` OR a list ``post_ids`` (the Profile
    grid's multi-select delete). The bulk path trashes every still-live post the
    caller owns in ONE UPDATE — no per-post round trip.

    Sets ``trashed_at`` (reason ``"self"``) instead of destroying rows, so they
    drop out of every feed/grid (the default manager hides them) but stay
    recoverable in the author's trash bin. ``Post.objects`` already excludes
    trashed posts, so the filter only ever trashes still-live rows — re-trashing
    is a no-op, making the call idempotent under client retries. Storage files
    are only removed when a post is purged from the trash.
    """
    # ── Bulk path: { "post_ids": [..] } ───────────────────────────────────
    raw_ids = request.data.get("post_ids")
    if raw_ids is not None:
        if not isinstance(raw_ids, (list, tuple)):
            return Response({"error": "post_ids must be a list"}, status=400)
        try:
            # Dedupe + coerce to ints; reject anything non-numeric outright.
            id_list = list({int(x) for x in raw_ids})
        except (TypeError, ValueError):
            return Response({"error": "post_ids must be integers"}, status=400)
        if not id_list:
            return Response({"error": "post_ids is empty"}, status=400)
        id_list = id_list[:MAX_BULK_TRASH]
        # Single UPDATE, scoped to the caller's own still-live posts.
        count = (
            Post.objects
            .filter(id__in=id_list, user=request.user)
            .update(trashed_at=timezone.now(), trashed_reason="self")
        )
        return Response({"status": "trashed", "count": count})

    # ── Single path: { "post_id": <int> } (unchanged) ─────────────────────
    post_id = request.data.get("post_id")
    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    try:
        post = Post.objects.get(id=post_id, user=request.user)
    except Post.DoesNotExist:
        return Response({"error": "Not found or not yours"}, status=404)

    post.trashed_at = timezone.now()
    post.trashed_reason = "self"
    post.save(update_fields=["trashed_at", "trashed_reason"])
    return Response({"status": "trashed"})



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_post_public(request):
    """
    Toggle is_public_override on a post inside a private page.
    Only the post's author may call this.

    When is_public_override=True the post surfaces in:
      • the home feed of all the author's followers
      • the reels feed (if it's a video reel)

    POST /posts/public-override/  { "post_id": <int> }
    Returns: { "is_public_override": true | false }
    """
    post_id = request.data.get("post_id")
    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    post = get_object_or_404(Post, id=post_id)

    # --------------------------------------------------
    # 🔐 OWNERSHIP CHECK
    # --------------------------------------------------
    if post.user != request.user:
        return Response({"error": "Not your post"}, status=403)

    # --------------------------------------------------
    # 🔒 ONLY MEANINGFUL FOR PRIVATE / SUPER-PRIVATE PAGE POSTS
    # --------------------------------------------------
    if not post.page or (
        not post.page.is_private and not post.page.is_super_private
    ):
        return Response(
            {"error": "Post is not in a private or super-private page"},
            status=400
        )

    post.is_public_override = not post.is_public_override
    post.save(update_fields=["is_public_override"])

    # H7: invalidate every follower's `suggested_feed_scores` cache so the
    # visibility change lands in their feed on the next /feed/ load,
    # instead of waiting up to the 5-minute TTL. The flip changes which
    # followers can see this private-page post (per
    # `post_visibility_q`'s `is_public_override AND user_id in followed_user_ids`
    # branch) -- both ON and OFF need the same invalidation, so we
    # delete unconditionally after the save. We DON'T touch
    # `feed_ctx:{uid}` here: that cache only holds the viewer's
    # social-graph data (followed users, blocked users, etc.) which
    # doesn't depend on per-post visibility flags. Same pattern
    # `create_post` already uses on new uploads.
    follower_ids = list(
        Follow.objects
        .filter(following=request.user)
        .values_list("follower_id", flat=True)
    )
    if follower_ids:
        cache.delete_many(
            [f"suggested_feed_scores:{uid}" for uid in follower_ids]
        )

    return Response({"is_public_override": post.is_public_override})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_trashed_posts(request):
    """The viewer's trashed posts, most recently trashed first. Backs the
    posts section of Profile → Trash. Uses ``Post.all_objects`` (the default
    manager hides trashed posts). Each entry carries a thumbnail + the origin
    page name (resolved even if that page is itself trashed) for display."""
    viewer = request.user
    posts = (
        Post.all_objects
        .filter(user=viewer, trashed_at__isnull=False)
        .select_related("page")
        .prefetch_related("media")
        .order_by("-trashed_at", "-id")
    )

    results = []
    for p in posts:
        media = list(p.media.all())
        first = media[0] if media else None
        is_video = bool(
            first
            and first.file
            and str(first.file.name).lower().endswith(VIDEO_EXTS)
        )
        thumb = None
        if first:
            src = first.thumbnail if first.thumbnail else first.file
            thumb = request.build_absolute_uri(src.url) if src else None
        # Full media file URL(s) so the trash bin's Download action can save the
        # originals (not just the thumbnail) to the device.
        media_list = [
            {
                "url": request.build_absolute_uri(mm.file.url) if mm.file else None,
                "is_video": bool(
                    mm.file and str(mm.file.name).lower().endswith(VIDEO_EXTS)
                ),
            }
            for mm in media
            if mm.file
        ]
        # Origin page (null once the page has been purged). FK access uses
        # Page's base manager, so a trashed page still resolves here.
        # ``page_active`` tells the trash UI whether the origin page is still
        # live (deleted_at is null) — if so, restore can offer to drop the post
        # straight back into it; if not, the user must pick another page.
        origin = p.page if p.page_id else None
        page_active = bool(origin and origin.deleted_at is None)
        results.append({
            "id": p.id,
            "thumbnail": thumb,
            "is_video": is_video,
            "media": media_list,
            "page_name": origin.name if origin else None,
            "page_id": origin.id if origin else None,
            "page_active": page_active,
            "trashed_at": p.trashed_at.isoformat() if p.trashed_at else None,
        })

    return Response({"results": results})


@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticated])
def purge_post(request):
    """Permanently delete one of the viewer's TRASHED posts (and its files).

    Owner-only, and only for posts already in the trash — a live post must be
    soft-deleted first. Deletes the media from storage, then the row.
    """
    post_id = request.data.get("post_id") or request.query_params.get("post_id")
    try:
        pid = int(post_id)
    except (TypeError, ValueError):
        return Response({"error": "post_id is required"}, status=400)

    try:
        post = Post.all_objects.get(id=pid)
    except Post.DoesNotExist:
        return Response({"error": "Not found"}, status=404)

    if post.user_id != request.user.id:
        return Response({"error": "Not your post"}, status=403)
    if post.trashed_at is None:
        return Response(
            {"error": "Post must be in the trash before it can be permanently deleted."},
            status=400,
        )

    purge_post_files(post)
    post.delete()
    return Response({"status": "purged"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def restore_post(request):
    """Restore one of the viewer's TRASHED posts into a page.

    Owner-only. The post must currently be in the trash, and the target page
    must be live and one the viewer is allowed to post in (same rule as a fresh
    upload). On success the post is attached to that page and un-trashed — there
    is no page-less restore, so ``page_id`` is required: the origin page may be
    gone, and even when it isn't the user explicitly chooses where it lands.
    """
    post_id = request.data.get("post_id")
    page_id = request.data.get("page_id")
    try:
        pid = int(post_id)
        target_page_id = int(page_id)
    except (TypeError, ValueError):
        return Response({"error": "post_id and page_id are required"}, status=400)

    try:
        post = Post.all_objects.get(id=pid)
    except Post.DoesNotExist:
        return Response({"error": "Not found"}, status=404)
    if post.user_id != request.user.id:
        return Response({"error": "Not your post"}, status=403)
    if post.trashed_at is None:
        return Response({"error": "Post is not in the trash."}, status=400)

    # Target page must be live (Page.objects hides trashed) and one the viewer
    # is allowed to post in — same gate as a fresh upload.
    page = get_object_or_404(Page, id=target_page_id)
    if not can_user_post_on_page(request.user, page):
        return Response({"error": "You can’t post in this page."}, status=403)

    post.page = page
    post.trashed_at = None
    post.trashed_reason = ""
    post.save(update_fields=["page", "trashed_at", "trashed_reason"])
    return Response({"status": "restored", "page_id": page.id})
