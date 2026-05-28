"""Responding to incoming follow requests: `approve_follow_request`,
`reject_follow_request`."""


from django.core.cache import cache
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Follow, FollowRequest, Notification
from ...services.push import push_to_user


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def approve_follow_request(request):
    request_id = request.data.get("request_id")

    if not request_id:
        return Response(
            {"error": "request_id required"},
            status=400
        )

    try:
        req = FollowRequest.objects.select_related(
            "requester",
            "target"
        ).get(id=request_id)
    except FollowRequest.DoesNotExist:
        return Response(
            {"error": "Follow request not found"},
            status=404
        )

    # --------------------------------------------------
    # 🔐 MUST BE TARGET USER
    # --------------------------------------------------
    if req.target != request.user:
        return Response(
            {"error": "Not allowed"},
            status=403
        )

    requester = req.requester
    target = req.target  # == request.user

    # --------------------------------------------------
    # 🚫 BLOCK CHECK (BOTH DIRECTIONS)
    # --------------------------------------------------
    if BlockedUser.objects.between(target, requester).exists():
        return Response(
            {"error": "Not allowed"},
            status=403
        )

    # --------------------------------------------------
    # 🔁 CREATE FOLLOW (IF NOT EXISTS)
    # --------------------------------------------------
    Follow.objects.get_or_create(
        follower=requester,
        following=target
    )

    # Invalidate the requester's suggested feed — they now follow `target`
    # so target's posts should leave suggestions.
    cache.delete(f"suggested_feed_scores:{requester.id}")

    # --------------------------------------------------
    # 🧹 CLEAN UP REQUEST
    # --------------------------------------------------
    req.delete()

    # --------------------------------------------------
    # 🔔 NOTIFICATION
    # --------------------------------------------------
    Notification.objects.create(
        recipient=requester,
        actor=target,
        notification_type="follow_approved"
    )

    push_to_user(
        requester,
        title="Follow request approved",
        body=f"{target.username} approved your follow request",
        extra_data={"type": "follow_approved", "actor_id": target.id},
    )

    return Response(
        {"status": "approved"},
        status=200
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reject_follow_request(request):
    requester_id = request.data.get('user_id')

    if not requester_id:
        return Response({"error": "user_id required"}, status=400)

    # Coerce safely: a non-numeric id would otherwise blow up the integer
    # FK filter below with a 500. Mirrors toggle_follow / not_interested.
    try:
        requester_id = int(requester_id)
    except (TypeError, ValueError):
        return Response({"error": "invalid user_id"}, status=400)

    FollowRequest.objects.filter(
        requester_id=requester_id,
        target=request.user
    ).delete()

    # Clean up the matching "follow_request" notification so the rejecting
    # user (its recipient) doesn't keep seeing a request that no longer
    # exists. The notification was created in toggle_follow with
    # recipient=target (== request.user here) and actor=requester. Mirrors
    # the notification cleanup the toggle_follow cancel path already does.
    Notification.objects.filter(
        recipient=request.user,
        actor_id=requester_id,
        notification_type="follow_request",
    ).delete()

    return Response({"status": "rejected"})


