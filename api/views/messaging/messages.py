"""Message operations: send (text + media), fetch, edit, delete, and new-message push."""

import mimetypes

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Conversation, ConversationHidden, Message, MessageMedia
from ...serializers import MessageSerializer
from ...services.chat import MESSAGE_EDIT_MAX_LEN, MESSAGE_EDIT_WINDOW
from ...services.push import push_to_user



def _push_new_message(convo, sender, text_preview: str, media_type=None):
    """Send a push to each recipient of a new message.

    Note: a previous version batched all recipients into one multicast for
    performance, but with multi-account routing each recipient needs their
    own for_user_id in the FCM data payload — otherwise a phone with two
    accounts logged in can't tell which account the message belongs to. So
    we fan out per recipient. For typical small group chats the extra round
    trips are negligible; for very large group chats this could be moved to
    a background queue (Celery, etc.) without changing behaviour.
    """
    if not text_preview:
        if media_type == 'image':
            text_preview = '📷 Photo'
        elif media_type == 'video':
            text_preview = '🎥 Video'
        elif media_type == 'audio':
            text_preview = '🎤 Voice message'
        else:
            text_preview = 'New message'

    recipients = list(convo.participants.exclude(id=sender.id))
    if not recipients:
        return

    for recipient in recipients:
        try:
            push_to_user(
                recipient,
                title=sender.username,
                body=text_preview,
                extra_data={
                    "type": "message",
                    "conversation_id": convo.id,
                    "sender_id": sender.id,
                },
            )
        except Exception:
            # Push failures must never break message delivery — keep going
            # so a problem with one recipient doesn't starve the others.
            pass



def _collect_typed_media(request):
    """Gather media files from the request (single ``media`` field, or indexed
    ``media_0..N`` for multi-send), detect each one's type, and validate.
    Returns ``(typed_files, error_response)``; ``error_response`` is None on
    success and a 400 ``Response`` if any file is an unsupported type."""
    single_media = request.FILES.get('media')
    indexed_media = []
    i = 0
    while True:
        f = request.FILES.get(f'media_{i}')
        if f is None:
            break
        indexed_media.append(f)
        i += 1

    if indexed_media:
        media_files = indexed_media
    elif single_media:
        media_files = [single_media]
    else:
        media_files = []

    def detect_type(f):
        mime = f.content_type or mimetypes.guess_type(f.name)[0] or ''
        if mime.startswith('image/'):
            return 'image'
        if mime.startswith('video/'):
            return 'video'
        if mime.startswith('audio/'):
            return 'audio'
        return None

    typed_files = []
    for f in media_files:
        t = detect_type(f)
        if t is None:
            return None, Response({"error": f"Unsupported media type for file: {f.name}"}, status=400)
        typed_files.append((f, t))
    return typed_files, None


def _resolve_conversation_for_send(request, conversation_id, target_user_id):
    """Resolve the target conversation: an existing one the user belongs to,
    or a freshly-created (or found) DM with ``target_user_id``. Enforces block
    rules in both directions. MUST be called inside a ``transaction.atomic()``
    block — it uses ``select_for_update``. Returns ``(convo, error_response)``."""
    # 🔹 CASE 1: EXISTING CONVERSATION
    if conversation_id:
        try:
            convo = (
                Conversation.objects
                .select_for_update()
                .prefetch_related("participants")
                .get(id=conversation_id)
            )
        except Conversation.DoesNotExist:
            return None, Response({"error": "Conversation not found"}, status=404)

        if request.user not in convo.participants.all():
            return None, Response({"error": "Not allowed"}, status=403)

        participants = convo.participants.all()
        if participants.count() == 2:
            other_user = participants.exclude(id=request.user.id).first()
            if BlockedUser.objects.between(request.user, other_user).exists():
                return None, Response({"error": "Not allowed"}, status=403)
        return convo, None

    # 🔹 CASE 2: FIRST MESSAGE → CREATE DM
    if not target_user_id:
        return None, Response({"error": "target_user_id required"}, status=400)
    try:
        other_user = User.objects.get(id=target_user_id)
    except User.DoesNotExist:
        return None, Response({"error": "User not found"}, status=404)

    if BlockedUser.objects.between(request.user, other_user).exists():
        return None, Response({"error": "Not allowed"}, status=403)

    # Lock both user rows in deterministic id order before the find-or-create
    # dance so two concurrent first-messages between the same pair don't both
    # create a fresh Conversation.
    list(
        User.objects
        .select_for_update()
        .filter(id__in=sorted({request.user.id, other_user.id}))
        .order_by('id')
    )
    convo = (
        Conversation.objects
        .filter(participants=request.user)
        .filter(participants=other_user)
        .annotate(num=Count("participants"))
        .filter(num=2)
        .first()
    )
    if not convo:
        convo = Conversation.objects.create()
        convo.participants.add(request.user, other_user)
    return convo, None


def _persist_message(convo, sender, text, typed_files, reply_to_obj):
    """Create the Message row plus its MessageMedia children. A single file
    uses the legacy ``Message.media`` field and is mirrored into MessageMedia
    by reference; multiple files fan out into ordered MessageMedia rows."""
    if len(typed_files) == 1:
        legacy_file, legacy_type = typed_files[0]
        message = Message.objects.create(
            conversation=convo,
            sender=sender,
            text=text,
            media=legacy_file,
            media_type=legacy_type,
            reply_to=reply_to_obj,
        )
        # Mirror the file into MessageMedia by REFERENCING the existing
        # storage path. Passing `file=message.media` to .create() would
        # call storage.save() a second time and (with FileSystemStorage)
        # produce a renamed duplicate on disk.
        mm = MessageMedia(
            message=message,
            media_type=legacy_type,
            order=0,
        )
        mm.file.name = message.media.name
        mm.save()
    else:
        message = Message.objects.create(
            conversation=convo,
            sender=sender,
            text=text,
            reply_to=reply_to_obj,
        )
        for order, (f, t) in enumerate(typed_files):
            MessageMedia.objects.create(
                message=message,
                file=f,
                media_type=t,
                order=order,
            )
    return message


def _build_message_fields(request, message):
    """Build the response/broadcast fields shared by the WS payload and the
    REST response: absolute media URLs, the sender avatar, and the compact
    reply_to payload."""
    media_items = [
        {"url": request.build_absolute_uri(item.file.url), "media_type": item.media_type}
        for item in message.media_items.all()
    ]

    media_url  = request.build_absolute_uri(message.media.url) if message.media else None
    media_type = message.media_type

    sender_profile = getattr(request.user, 'userprofile', None)
    sender_avatar = (
        request.build_absolute_uri(sender_profile.avatar.url)
        if sender_profile and sender_profile.avatar
        else None
    )

    reply_to_data = None
    if message.reply_to:
        r = message.reply_to
        r_media_url = None
        if not r.is_deleted and r.media:
            r_media_url = request.build_absolute_uri(r.media.url)
        if not r_media_url and not r.is_deleted:
            first = r.media_items.first()
            if first:
                r_media_url = request.build_absolute_uri(first.file.url)
        reply_to_data = {
            "id":         r.id,
            "sender_id":  r.sender_id,
            "sender":     r.sender.username,
            "text":       "" if r.is_deleted else r.text,
            "media_url":  r_media_url,
            "media_type": r.media_type or (r.media_items.first().media_type if r.media_items.exists() else None),
            "is_deleted": r.is_deleted,
        }

    return {
        "media_items":   media_items,
        "media_url":     media_url,
        "media_type":    media_type,
        "sender_avatar": sender_avatar,
        "reply_to_data": reply_to_data,
    }


def _broadcast_new_message(convo, request, message, fields):
    """Relay the new message to the conversation's WS group. Fires
    unconditionally (text or media): clients fall back to this REST endpoint
    whenever the socket is down, and recipients dedup by message id, so a
    text-only REST send must still reach other participants in real time."""
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"chat_{convo.id}",
        {
            "type": "chat.media_message",
            "payload": {
                "type": "message.new",
                "message": {
                    "id":             message.id,
                    "sender_id":      message.sender_id,
                    "sender":         request.user.username,
                    "sender_avatar":  fields["sender_avatar"],
                    "text":           message.text,
                    "created_at":     message.created_at.isoformat(),
                    "is_deleted":     False,
                    "is_edited":      False,
                    "last_edited_at": None,
                    "read_by":        [],
                    "is_mine":        False,
                    "media_url":      fields["media_url"],
                    "media_type":     fields["media_type"],
                    "media_items":    fields["media_items"],
                    "reply_to":       fields["reply_to_data"],
                }
            }
        }
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def send_message(request):
    conversation_id = request.data.get('conversation_id')
    target_user_id  = request.data.get('target_user_id')
    text            = (request.data.get('text') or '').strip()
    reply_to_id     = request.data.get('reply_to_id')

    typed_files, media_error = _collect_typed_media(request)
    if media_error:
        return media_error
    if not text and not typed_files:
        return Response({"error": "Message empty"}, status=400)

    with transaction.atomic():
        convo, convo_error = _resolve_conversation_for_send(
            request, conversation_id, target_user_id
        )
        if convo_error:
            return convo_error

        # Restore soft-deleted conversation visibility
        ConversationHidden.objects.filter(
            conversation=convo,
            user__in=convo.participants.all()
        ).delete()

        reply_to_obj = None
        if reply_to_id:
            try:
                reply_to_obj = Message.objects.get(id=reply_to_id, conversation=convo)
            except Message.DoesNotExist:
                pass

        message = _persist_message(convo, request.user, text, typed_files, reply_to_obj)

        # ✅ Bump updated_at so conversation list re-sorts correctly
        Conversation.objects.filter(id=convo.id).update(updated_at=timezone.now())

    fields = _build_message_fields(request, message)
    _broadcast_new_message(convo, request, message, fields)

    # ✅ Push notifications to other participants
    first_media_type = typed_files[0][1] if typed_files else None
    _push_new_message(convo, request.user, text, first_media_type)

    return Response(
        {
            "conversation_id": convo.id,
            "message": {
                "id":             message.id,
                "sender_id":      message.sender_id,
                "sender":         request.user.username,
                "sender_avatar":  fields["sender_avatar"],
                "text":           message.text,
                "created_at":     message.created_at,
                "media_url":      fields["media_url"],
                "media_type":     fields["media_type"],
                "media_items":    fields["media_items"],
                "is_mine":        True,
                "is_deleted":     False,
                "is_edited":      False,
                "last_edited_at": None,
                "read_by":        [],
                "reply_to":       fields["reply_to_data"],
            }
        },
        status=201
    )



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_messages(request):
    conversation_id = request.query_params.get('conversation_id')
    if not conversation_id:
        return Response({"error": "conversation_id required"}, status=400)

    # ✅ NEW: pagination params — defensively parsed so a bogus value
    # returns 400 instead of bubbling up as a 500.
    raw_limit = request.query_params.get('limit', 30)
    try:
        limit = min(max(int(raw_limit), 1), 100)
    except (TypeError, ValueError):
        return Response({"error": "limit must be an integer"}, status=400)

    raw_before = request.query_params.get('before')
    before_id = None
    if raw_before is not None and raw_before != '':
        try:
            before_id = int(raw_before)
        except (TypeError, ValueError):
            return Response({"error": "before must be an integer"}, status=400)

    try:
        convo = (
            Conversation.objects
            .prefetch_related("participants")
            .get(id=conversation_id)
        )
    except Conversation.DoesNotExist:
        return Response({"error": "Conversation not found"}, status=404)

    # 🔐 MUST BE A PARTICIPANT
    if request.user not in convo.participants.all():
        return Response({"error": "Not allowed"}, status=403)

    # 🚫 BLOCK CHECK (AGAINST ALL OTHER PARTICIPANTS)
    for other_user in convo.participants.exclude(id=request.user.id):
        if BlockedUser.objects.between(request.user, other_user).exists():
            # 👻 Appear as if convo doesn't exist
            return Response({"error": "Conversation not found"}, status=404)

    qs = (
        Message.objects
        .filter(conversation=convo)
        .select_related("sender", "sender__userprofile", "reply_to__sender")
        .prefetch_related("read_by", "reactions", "media_items")
        .order_by("-created_at")  # newest first for efficient cursor slicing
    )

    if before_id is not None:
        qs = qs.filter(id__lt=before_id)

    messages = list(qs[:limit])
    # ✅ Return in chronological order for the client
    messages.reverse()

    has_more = qs.filter(id__lt=messages[0].id).exists() if messages else False

    data = MessageSerializer(
        messages, many=True, context={'request': request, 'viewer': request.user}
    ).data

    return Response({
        "results":   data,
        "has_more":  has_more,
        # client passes this as `before` on next page request
        "oldest_id": messages[0].id if messages else None,
    })



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def edit_message(request):
    """
    Edit the text of your own non-deleted message.
    POST /auth/messages/edit/  { message_id, text }
    Broadcasts message.edited to the conversation WS group.

    Constraints:
      • Max ``MESSAGE_EDIT_MAX_LEN`` characters.
      • Must be edited within ``MESSAGE_EDIT_WINDOW`` of the original send.
    """
    message_id = request.data.get("message_id")
    new_text   = (request.data.get("text") or "").strip()

    if not message_id or not new_text:
        return Response({"error": "message_id and text are required."}, status=400)

    if len(new_text) > MESSAGE_EDIT_MAX_LEN:
        return Response(
            {"error": f"Message too long (max {MESSAGE_EDIT_MAX_LEN} chars)."},
            status=400,
        )

    try:
        message = Message.objects.select_related("conversation").get(id=message_id)
    except Message.DoesNotExist:
        return Response({"error": "Message not found."}, status=404)

    if message.sender_id != request.user.id:
        return Response({"error": "Not your message."}, status=403)

    # Block check (mirrors send_message at line ~322): in a two-person DM,
    # if you and the other participant have fallen on either side of a
    # block since the message was sent, the edit is denied — the
    # message.edited WS broadcast would otherwise update the text on the
    # blocker's screen. Group convos are left alone (mirrors send_message's
    # "only enforce block in 2-person DMs" semantics).
    other_participants = message.conversation.participants.exclude(id=request.user.id)
    if other_participants.count() == 1:
        other = other_participants.first()
        if BlockedUser.objects.between(request.user, other).exists():
            return Response({"error": "Not allowed."}, status=403)

    if message.is_deleted:
        return Response({"error": "Cannot edit a deleted message."}, status=400)

    if timezone.now() - message.created_at > MESSAGE_EDIT_WINDOW:
        return Response(
            {"error": "Edit window has expired."},
            status=400,
        )

    message.text           = new_text
    message.is_edited      = True
    message.last_edited_at = timezone.now()
    message.save(update_fields=["text", "is_edited", "last_edited_at"])

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"chat_{message.conversation_id}",
        {
            "type": "broadcast",
            "payload": {
                "type":           "message.edited",
                "message_id":     message.id,
                "text":           new_text,
                "edited_by":      request.user.id,
                "last_edited_at": message.last_edited_at.isoformat(),
            },
        },
    )

    return Response({
        "status":         "edited",
        "message_id":     message.id,
        "text":           new_text,
        "last_edited_at": message.last_edited_at.isoformat(),
    })



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delete_message(request):
    """
    Soft-delete your own message.
    POST /auth/messages/delete/  { message_id }
    Broadcasts message.deleted to the conversation WS group.

    Mirrors the WebSocket `message.delete` handler in ChatConsumer so
    deletion still works when the client's socket is closed or reconnecting
    — without this REST path, tapping Delete during a network blip silently
    did nothing (the menu would close as if it had worked, leaving the
    message visible on every recipient's screen).
    """
    message_id = request.data.get("message_id")
    if not message_id:
        return Response({"error": "message_id required."}, status=400)

    try:
        message = Message.objects.select_related("conversation").get(id=message_id)
    except Message.DoesNotExist:
        return Response({"error": "Message not found."}, status=404)

    if message.sender_id != request.user.id:
        return Response({"error": "Not your message."}, status=403)

    # Block check (mirrors send_message at line ~322): in a two-person DM,
    # if you and the other participant have fallen on either side of a
    # block since the message was sent, soft-delete is denied — the
    # message.deleted WS broadcast would otherwise replace your message
    # with "Message deleted" on the blocker's screen, which is the kind of
    # harassment vector the block is supposed to prevent. Group convos are
    # left alone for parity with send_message.
    other_participants = message.conversation.participants.exclude(id=request.user.id)
    if other_participants.count() == 1:
        other = other_participants.first()
        if BlockedUser.objects.between(request.user, other).exists():
            return Response({"error": "Not allowed."}, status=403)

    # Idempotent: a retry after a flaky network shouldn't 400 just because
    # the first attempt already landed. Treat repeated deletes as a success
    # so the client UI can converge without surfacing a spurious error.
    if message.is_deleted:
        return Response({"status": "deleted", "message_id": message.id})

    # Soft-delete + hard-delete the media blobs (legacy `media` field and any
    # MessageMedia rows). Shared with the WS consumer via Message.soft_delete()
    # so a deleted photo/video isn't left downloadable and storage doesn't
    # accumulate orphans (M4).
    message.soft_delete()

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"chat_{message.conversation_id}",
        {
            "type": "broadcast",
            "payload": {
                "type":       "message.deleted",
                "message_id": message.id,
                "deleted_by": request.user.id,
            },
        },
    )

    return Response({"status": "deleted", "message_id": message.id})
