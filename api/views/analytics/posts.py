"""Post-engagement analytics: view, dwell, share."""


from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Activity
from ...services.activity import log_activity


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_post_view(request):
    """Body: post_id, duration_seconds, surface (optional), channel (optional)."""
    post_id = request.data.get("post_id")
    duration = request.data.get("duration_seconds", 0)
    surface = request.data.get("surface", "post_detail")
    channel = request.data.get("channel", "direct")

    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    try:
        post_id = int(post_id)
        duration = float(duration)
    except (TypeError, ValueError):
        return Response({"error": "invalid input"}, status=400)

    log_activity(
        request.user,
        "post_view",
        post_id=post_id,
        duration_seconds=max(0.0, duration),
        surface=surface if surface in dict(Activity.SURFACE_CHOICES) else "post_detail",
        channel=channel if channel in dict(Activity.CHANNEL_CHOICES) else "direct",
    )
    return Response({"status": "logged"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_post_dwell(request):
    """Body: post_id, duration_seconds, surface."""
    post_id = request.data.get("post_id")
    duration = request.data.get("duration_seconds", 0)
    surface = request.data.get("surface", "home")

    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    try:
        post_id = int(post_id)
        duration = float(duration)
    except (TypeError, ValueError):
        return Response({"error": "invalid input"}, status=400)

    log_activity(
        request.user,
        "post_dwell",
        post_id=post_id,
        duration_seconds=max(0.0, duration),
        surface=surface if surface in dict(Activity.SURFACE_CHOICES) else "home",
    )
    return Response({"status": "logged"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_post_share(request):
    """Body: post_id, channel (optional)."""
    post_id = request.data.get("post_id")
    channel = (request.data.get("channel") or "direct").lower()
    if channel not in dict(Activity.CHANNEL_CHOICES):
        channel = "direct"

    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return Response({"error": "invalid post_id"}, status=400)

    log_activity(request.user, "post_share", post_id=post_id, channel=channel)
    return Response({"status": "logged"})
