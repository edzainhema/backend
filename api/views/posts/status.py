"""Lightweight HLS-readiness poll for the upload banner's 'Finalizing video…'
state. The post is already live and playable on its MP4 by the time this is
polled; this just reports whether the background HLS encode (process_post_media)
has finished so the banner can flip from 'Finalizing video…' to 'Posted!'."""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Post

# PostMedia has no media_type column; sniff the stored filename, same as the
# upload path and the backfill command.
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")


def _is_video(media):
    name = (media.file.name or "").lower()
    return any(name.endswith(ext) for ext in VIDEO_EXTS)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def post_hls_status(request, post_id):
    """`{ "ready": bool, "hls": <master playlist url|null> }`.

    `ready` is true once every video media on the post has an `hls_master`.
    Owner-scoped; an unknown id, a post that isn't the caller's, or a post with
    no video all report `ready: true` so the client's poll never hangs."""
    try:
        post = Post.objects.prefetch_related("media").get(id=int(post_id))
    except (Post.DoesNotExist, ValueError, TypeError):
        return Response({"ready": True, "hls": None})

    if post.user_id != request.user.id:
        return Response({"ready": True, "hls": None})

    videos = [m for m in post.media.all() if _is_video(m)]
    if not videos:
        return Response({"ready": True, "hls": None})

    ready = all(bool(m.hls_master) for m in videos)
    hls = None
    if ready:
        first = videos[0]
        hls = (
            request.build_absolute_uri(first.hls_master.url)
            if first.hls_master else None
        )
    return Response({"ready": ready, "hls": hls})
