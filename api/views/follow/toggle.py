"""Follow / unfollow a user (`toggle_follow`) — public follows, follow
requests for private accounts, and the notify/push fan-out."""


from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Follow, FollowRequest, Notification
from ...services.push import push_to_user


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_follow(request):
    target_user_id = request.data.get("user_id")

    if not target_user_id:
        return Response(
            {"error": "user_id required"},
            status=400
        )

    # Coerce once, safely: a non-numeric user_id used to reach the bare
    # int(...) below and raise ValueError -> unhandled 500. Mirror the
    # try/except pattern used by not_interested et al. and return 400. The
    # validated int is then reused for the self-check and the lookup.
    try:
        target_user_id = int(target_user_id)
    except (TypeError, ValueError):
        return Response(
            {"error": "invalid user_id"},
            status=400
        )

    if target_user_id == request.user.id:
        return Response(
            {"error": "Cannot follow yourself"},
            status=400
        )

    try:
        target_user = User.objects.get(id=target_user_id)
    except User.DoesNotExist:
        return Response(
            {"error": "User not found"},
            status=404
        )

    # --------------------------------------------------
    # 🚫 BLOCK CHECK (BOTH DIRECTIONS)
    # --------------------------------------------------
    if BlockedUser.objects.between(request.user, target_user).exists():
        return Response(
            {"error": "Not allowed"},
            status=403
        )

    # --------------------------------------------------
    # 🔐 PRIVACY CHECK
    # --------------------------------------------------
    user_profile = getattr(target_user, "userprofile", None)
    is_private = user_profile.is_private if user_profile else False

    # --------------------------------------------------
    # 🔁 UNFOLLOW IF ALREADY FOLLOWING
    # --------------------------------------------------
    existing_follow = Follow.objects.filter(
        follower=request.user,
        following=target_user
    ).first()

    if existing_follow:
        existing_follow.delete()
        # Remove the matching "X started following you" notification so the
        # target doesn't keep seeing a follow that no longer exists. Mirrors
        # the cleanup the cancel-follow-request branch below already does.
        Notification.objects.filter(
            recipient=target_user,
            actor=request.user,
            notification_type="follow",
        ).delete()
        # Invalidate suggested feed — the unfollowed user's posts should
        # now be eligible to reappear as suggestions on the next load.
        cache.delete(f"suggested_feed_scores:{request.user.id}")
        return Response({"status": "unfollowed"})

    # --------------------------------------------------
    # 🔒 PRIVATE ACCOUNT → FOLLOW REQUEST (toggle)
    # --------------------------------------------------
    if is_private:
        existing_request = FollowRequest.objects.filter(
            requester=request.user,
            target=target_user
        ).first()

        # Second tap on "Follow request sent" → cancel the pending request
        if existing_request:
            existing_request.delete()
            # Clean up the matching notification so it doesn't linger
            Notification.objects.filter(
                recipient=target_user,
                actor=request.user,
                notification_type="follow_request"
            ).delete()
            return Response({"status": "request_cancelled"})

        FollowRequest.objects.create(
            requester=request.user,
            target=target_user
        )
        Notification.objects.create(
            recipient=target_user,
            actor=request.user,
            notification_type="follow_request"
        )
        # 🔔 PUSH NOTIFICATION
        push_to_user(
            target_user,
            title="New follow request",
            body=f"{request.user.username} requested to follow you",
            extra_data={"type": "follow_request", "actor_id": request.user.id},
        )

        return Response({"status": "requested"})

    # --------------------------------------------------
    # ✅ PUBLIC ACCOUNT → FOLLOW IMMEDIATELY
    # --------------------------------------------------
    Follow.objects.create(
        follower=request.user,
        following=target_user
    )

    # Invalidate suggested feed — the newly followed user's posts should
    # no longer appear as suggestions on the next load.
    cache.delete(f"suggested_feed_scores:{request.user.id}")

    Notification.objects.create(
        recipient=target_user,
        actor=request.user,
        notification_type="follow"
    )

    # 🔔 PUSH NOTIFICATION
    push_to_user(
        target_user,
        title="New follower",
        body=f"{request.user.username} started following you",
        extra_data={"type": "follow", "actor_id": request.user.id},
    )

    return Response({"status": "following"})


