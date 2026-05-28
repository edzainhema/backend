"""Watch-time analytics: video and reel watch events."""


import math

from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Post, ReelWatch, VideoWatch
from ...services.activity import log_activity


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_video_watch(request):
    viewer = request.user

    watch_id = request.data.get("watch_id")
    duration_seconds = request.data.get("duration_seconds")

    if watch_id is not None and duration_seconds is not None:
        try:
            watch = VideoWatch.objects.get(
                id=int(watch_id),
                viewer=viewer,
            )
        except (VideoWatch.DoesNotExist, ValueError):
            return Response({"error": "Watch not found"}, status=404)

        watch.duration_seconds = max(0, int(duration_seconds))
        watch.save(update_fields=["duration_seconds"])

        return Response({"status": "updated"})

    post_id = request.data.get("post_id")
    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return Response({"error": "Invalid post_id"}, status=400)

    post = get_object_or_404(Post, id=post_id)

    channel = request.data.get("channel", "direct")
    if channel not in {"direct", "link", "search"}:
        channel = "direct"
    surface = request.data.get("surface", "reels")
    if surface not in {"home", "explore", "profile", "page", "reels", "search", "search_results"}:
        surface = "reels"

    watch = VideoWatch.objects.create(
        viewer=viewer,
        post=post,
        channel=channel,
        surface=surface,
    )

    return Response({"status": "logged", "watch_id": watch.id})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_reel_watch(request):
    """Body: post_id, duration_seconds, video_duration_seconds (optional),
    watched_to_end, is_rewatch, is_skip."""
    post_id = request.data.get("post_id")
    duration = request.data.get("duration_seconds", 0)

    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    try:
        post_id = int(post_id)
        duration = float(duration)
    except (TypeError, ValueError):
        return Response({"error": "invalid input"}, status=400)

    # Total length of the video (NOT the watch time). Lets the feed ranker
    # score this watch as a completion ratio (watch / length) instead of
    # against an arbitrary fixed reference. Optional + defensively validated:
    # a missing / junk / non-positive value is simply not stored, and the
    # ranker falls back to its legacy reference for those rows.
    video_seconds = None
    raw_video = request.data.get("video_duration_seconds")
    if raw_video is not None:
        try:
            v = float(raw_video)
        except (TypeError, ValueError):
            v = None
        if v is not None and math.isfinite(v) and v > 0:
            # Cap at the 5-minute upload ceiling (+ slack) so a garbage
            # payload can't store a wild length.
            video_seconds = min(v, 600.0)

    watched_to_end = bool(request.data.get("watched_to_end", False))
    is_rewatch = bool(request.data.get("is_rewatch", False))
    is_skip = bool(request.data.get("is_skip", False))

    reel_meta = {}
    if video_seconds is not None:
        reel_meta["video_seconds"] = video_seconds

    log_activity(
        request.user,
        "reel_watch",
        post_id=post_id,
        duration_seconds=max(0.0, duration),
        watched_to_end=watched_to_end,
        is_rewatch=is_rewatch,
        is_skip=is_skip,
        surface="reels",
        **({"metadata": reel_meta} if reel_meta else {}),
    )
    if watched_to_end:
        log_activity(request.user, "reel_complete", post_id=post_id, surface="reels")
    if is_rewatch:
        log_activity(request.user, "reel_rewatch", post_id=post_id, surface="reels")
    if is_skip:
        log_activity(request.user, "reel_skip", post_id=post_id, surface="reels")

    try:
        rw, _ = ReelWatch.objects.get_or_create(
            user=request.user,
            post_id=post_id,
            defaults={"seconds_watched": max(0.0, duration)},
        )
        rw.seconds_watched = max(rw.seconds_watched, max(0.0, duration))
        rw.save(update_fields=["seconds_watched", "updated_at"])
    except Exception:
        pass

    return Response({"status": "logged"})


