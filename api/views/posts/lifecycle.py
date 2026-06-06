"""Post lifecycle: delete and public/visibility toggle."""


from django.core.cache import cache
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Follow, Post

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delete_post(request):
    post_id = request.data.get("post_id")
    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    try:
        post = Post.objects.get(id=post_id, user=request.user)
    except Post.DoesNotExist:
        return Response({"error": "Not found or not yours"}, status=404)

    post.delete()
    return Response({"status": "deleted"})



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
