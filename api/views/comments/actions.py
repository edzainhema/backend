"""Actions on an existing comment: delete, and like/unlike (with a throttled
push so rapid like/unlike taps don't spam the recipient's lock screen)."""


from django.core.cache import cache
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Comment, CommentLike, Notification
from ...services.push import push_to_user
from ...services.feed_helpers import viewer_can_see_post

# Throttle window for comment-like push notifications: within this many
# seconds of having already pushed for (actor, comment), suppress follow-up
# pushes. The in-app notification still updates; only the lock-screen push
# is throttled, so like/unlike flip-flopping doesn't spam the recipient.
COMMENT_LIKE_PUSH_THROTTLE_SECONDS = 30


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delete_comment(request):
    comment_id = request.data.get("comment_id")
    if not comment_id:
        return Response({"error": "comment_id required"}, status=400)

    comment = get_object_or_404(Comment, id=comment_id)

    if comment.user != request.user:
        return Response({"error": "Not allowed"}, status=403)

    # Hard-delete the attached blob from storage before clearing the field.
    # Soft-deleting only the DB row leaves the underlying image/video sitting
    # in the media bucket forever, which adds up to real money for any
    # popular post. The save=False keeps this in our single .save() below.
    if comment.file:
        try:
            comment.file.delete(save=False)
        except Exception:
            # Don't fail the soft-delete if storage hiccups; a background
            # sweeper can still pick up the orphan from the (now NULL) ref.
            pass

    comment.is_deleted = True
    comment.deleted_at = timezone.now()
    comment.text = ""
    comment.file = None
    comment.save(update_fields=["is_deleted", "deleted_at", "text", "file"])

    return Response({"status": "deleted"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_comment_like(request):
    comment_id = request.data.get("comment_id")

    if not comment_id:
        return Response(
            {"error": "comment_id required"},
            status=400
        )

    comment = get_object_or_404(
        Comment.objects.select_related(
            "post", "post__user", "post__user__userprofile", "post__page",
            "user",
        ),
        id=comment_id,
    )
    comment_owner = comment.user

    # Must be allowed to see the underlying post (same gate as the comments
    # list / create) — otherwise comment ids could be probed/liked on posts
    # the viewer can't see. viewer_can_see_post returns 404 (existence-hiding);
    # the separate check below still 403s when the COMMENT's author (who may
    # differ from the post author) has blocked the viewer.
    if not viewer_can_see_post(request.user, comment.post):
        return Response({"error": "Not found"}, status=404)

    if BlockedUser.objects.between(request.user, comment_owner).exists():
        return Response(
            {"error": "Not allowed"},
            status=403
        )

    like, created = CommentLike.objects.get_or_create(
        user=request.user,
        comment=comment
    )

    if not created:
        like.delete()
        # Remove any prior "X liked your comment" notification for this
        # actor/comment so the recipient's feed doesn't keep a phantom
        # like notification after the user changes their mind.
        Notification.objects.filter(
            recipient=comment_owner,
            actor=request.user,
            notification_type="comment_like",
            comment=comment,
        ).delete()
        return Response({"liked": False})

    if comment_owner != request.user:
        Notification.objects.create(
            recipient=comment_owner,
            actor=request.user,
            notification_type="comment_like",
            media=comment.post,
            comment=comment,
        )
        # Throttle the push (not the in-app notification). The in-app
        # notification is created/deleted in lockstep with the like row,
        # so it'll already accurately reflect the latest state. The push,
        # by contrast, can't be unsent once it's gone to APNs/FCM — and
        # a spammy like/unlike sender otherwise floods the recipient's
        # lock screen even though their in-app history shows nothing.
        # cache.add() is atomic: it sets the key only if absent and
        # returns True when it wins the race, so we push exactly once per
        # (actor, comment) per throttle window.
        push_key = f"clike_push:{request.user.id}:{comment.id}"
        if cache.add(push_key, True, timeout=COMMENT_LIKE_PUSH_THROTTLE_SECONDS):
            push_to_user(
                comment_owner,
                title="New like",
                body=f"{request.user.username} liked your comment",
                extra_data={
                    "type": "comment_like",
                    "post_id": comment.post.id,
                    "comment_id": comment.id,
                    "actor_id": request.user.id,
                },
            )

    return Response({"liked": True})
