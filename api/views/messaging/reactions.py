"""Message reactions: toggle a reaction and summarise a message's reactions."""


from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Message, MessageReaction

def _reaction_summary(message) -> dict:
    """Returns { emoji: count } for all reactions on a message."""
    qs = (
        MessageReaction.objects
        .filter(message=message)
        .values("emoji")
        .annotate(count=Count("id"))
    )
    return {row["emoji"]: row["count"] for row in qs}



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def react_to_message(request):
    """
    Toggle an emoji reaction on a message.

    Body: { "message_id": <int>, "emoji": "❤️" }

    Behaviour:
      • If the user has NO reaction on this message  → add it.
      • If the user reacted with the SAME emoji      → remove it (toggle off).
      • If the user reacted with a DIFFERENT emoji   → replace it.

    After saving, broadcasts a `message.reaction` event over the conversation's
    WebSocket group so all connected clients update in real time.
    """
    message_id = request.data.get("message_id")
    emoji      = (request.data.get("emoji") or "").strip()

    if not message_id or not emoji:
        return Response({"error": "message_id and emoji are required."}, status=400)

    try:
        message = Message.objects.select_related("conversation").get(id=message_id)
    except Message.DoesNotExist:
        return Response({"error": "Message not found."}, status=404)

    if not message.conversation.participants.filter(id=request.user.id).exists():
        return Response({"error": "Forbidden."}, status=403)

    # Block check (mirrors send_message at line ~322): a block relationship
    # between you and the message's sender — in either direction — should
    # prevent the reaction. Without this, a blocked user could keep reacting
    # to messages from someone who'd already cut them off, and the
    # message.reaction WS broadcast would propagate the reaction to the
    # blocker's screen. Self-reactions skip the check (can't block yourself).
    if message.sender_id != request.user.id:
        if BlockedUser.objects.between(request.user, message.sender_id).exists():
            return Response({"error": "Forbidden."}, status=403)

    if message.is_deleted:
        return Response({"error": "Cannot react to a deleted message."}, status=400)

    # Race-safe toggle. The previous get/create pattern raced under concurrent
    # taps from the same user (both miss the get, both create → IntegrityError
    # from the (message, user) unique_together). Doing the work inside an
    # atomic block with select_for_update on the existing row — or
    # update_or_create when none exists — serializes the toggle.
    with transaction.atomic():
        existing = (
            MessageReaction.objects
            .select_for_update()
            .filter(message=message, user=request.user)
            .first()
        )
        if existing is None:
            try:
                MessageReaction.objects.create(
                    message=message, user=request.user, emoji=emoji
                )
                action = "added"
            except IntegrityError:
                # Another concurrent request beat us to it — fall through
                # and treat as a replace/remove from the now-existing row.
                existing = MessageReaction.objects.select_for_update().get(
                    message=message, user=request.user
                )
                if existing.emoji == emoji:
                    existing.delete()
                    action = "removed"
                else:
                    existing.emoji = emoji
                    existing.save(update_fields=["emoji"])
                    action = "replaced"
        elif existing.emoji == emoji:
            existing.delete()
            action = "removed"
        else:
            existing.emoji = emoji
            existing.save(update_fields=["emoji"])
            action = "replaced"

    reactions = _reaction_summary(message)

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"chat_{message.conversation_id}",
        {
            "type": "broadcast",
            "payload": {
                "type":       "message.reaction",
                "message_id": message.id,
                "user_id":    request.user.id,
                "emoji":      emoji,
                "action":     action,
                "reactions":  reactions,
            },
        },
    )

    return Response({
        "action":     action,
        "message_id": message.id,
        "reactions":  reactions,
    })
