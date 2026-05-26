"""Post lifecycle: delete and public/visibility toggle."""


from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Post

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
    # 🔒 ONLY MEANINGFUL FOR PRIVATE-PAGE POSTS
    # --------------------------------------------------
    if not post.page or not post.page.is_private:
        return Response(
            {"error": "Post is not in a private page"},
            status=400
        )

    post.is_public_override = not post.is_public_override
    post.save(update_fields=["is_public_override"])

    return Response({"is_public_override": post.is_public_override})
