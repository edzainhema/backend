"""Message operations: send (text + media), fetch, edit, delete, and new-message push."""

import mimetypes

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.decorators import api_view, parser_classes, permission_classes, throttle_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import (
    BlockedUser, Conversation, ConversationHidden,
    ConversationParticipantState, Follow, Message, MessageMedia,
)
from ...serializers import MessageSerializer
from ...services.chat import MAX_FILES_PER_MESSAGE, MESSAGE_EDIT_MAX_LEN, MESSAGE_EDIT_WINDOW, MESSAGE_MAX_LEN
from ...services.conversations import (
    _count_request_creation,
    check_request_throttle,
    ensure_participant_states,
    get_accepted_recipient_ids,
    mark_accepted,
)
from ...services.media import validate_uploaded_media_file
from ...services.push import push_to_user
from ...services.throttles import SendMessageRateThrottle


def _push_new_message(convo, sender, text_preview: str, media_type=None):
    """Send a push to each recipient of a new message.

    Note: a previous version batched all recipients into one multicast for
    performance, but with multi-account routing each recipient needs their
    own for_user_id in the FCM data payload -- otherwise a phone with two
    accounts logged in can't tell which account the message belongs to. So
    we fan out per recipient. For typical small group chats the extra round
    trips are negligible; for very large group chats this could be moved to
    a background queue (Celery, etc.) without changing behaviour.

    M7: recipients whose ConversationParticipantState.is_accepted=False
    are skipped -- a message in a request thread is silent on their
    device until they accept. The badge on the requests tab handles
    notification UX; pushing would defeat the spam-suppression point
    of the requests inbox entirely.
    """
    if not text_preview:
        if media_type == 'image':
            text_preview = 'Photo'
        elif media_type == 'video':
            text_preview = 'Video'
        elif media_type == 'audio':
            text_preview = 'Voice message'
        else:
            text_preview = 'New message'

    recipients = list(convo.participants.exclude(id=sender.id))
    if not recipients:
        return

    # M7: filter the recipient list down to those whose state row is
    # is_accepted=True. Missing rows (legacy / grandfathered) default
    # to True so they keep getting pushes, matching pre-M7 behaviour.
    accepted_ids = get_accepted_recipient_ids(convo.id, sender.id)
    recipients = [r for r in recipients if r.id in accepted_ids]
    if not recipients:
        return

    for recipient in recipients:
        try:
            # M11: pass actor=sender so push_to_user can skip the push
            # for recipients who've muted the sender. Without this, a
            # muted DM still buzzes the recipient's phone -- the mute
            # only suppresses the FEED, never the notification, which
            # contradicts the typical product semantics.
            push_to_user(
                recipient,
                title=sender.username,
                body=text_preview,
                extra_data={
                    "type": "message",
                    "conversation_id": convo.id,
                    "sender_id": sender.id,
                },
                actor=sender,
            )
        except Exception:
            pass


def _collect_typed_media(request):
    """Gather DM attachments and run each through the shared upload-safety
    pipeline (audit B5).

    Previously this only inspected the client-supplied Content-Type header
    to classify each file, then handed the raw bytes to FileField.save() --
    no size cap, no magic-byte verification, no decompression-bomb guard.
    A user could DM a 2 GB file with Content-Type: image/png and it would
    land in media/message_media/ unchallenged. Routing every file through
    `validate_uploaded_media_file` closes that gap and keeps DM uploads
    on the same hardened path post / comment uploads already use.

    DMs accept all three media kinds (image / video / audio for voice
    notes). The validator returns the detected kind, which we persist as
    Message.media_type / MessageMedia.media_type without re-detecting.
    """
    single_media = request.FILES.get('media')
    # H5: bound the collection loop at MAX_FILES_PER_MESSAGE + 1 so we
    # iterate at most one slot beyond the cap, then explicitly reject
    # if another slot exists past that. Without this, the loop ran
    # until it found a missing index -- a client posting media_0..
    # media_99 would inflate the upload buffer + disk for all 100
    # files before any per-endpoint validation. Django's
    # DATA_UPLOAD_MAX_NUMBER_FILES caps at 100 by default which
    # bounds the worst case at the framework level, but our 10 here
    # matches the per-endpoint product cap that posts/create.py uses.
    indexed_media = []
    for i in range(MAX_FILES_PER_MESSAGE):
        f = request.FILES.get(f'media_{i}')
        if f is None:
            break
        indexed_media.append(f)
    # Sniff for one slot past the cap. If it's there the client tried
    # to overshoot -- reject explicitly instead of silently dropping
    # everything after the cap (which would leave the user confused
    # about why some attachments "didn't send").
    if request.FILES.get(f'media_{MAX_FILES_PER_MESSAGE}') is not None:
        return None, Response(
            {"error": f"Too many attachments (max {MAX_FILES_PER_MESSAGE})."},
            status=400,
        )

    if indexed_media:
        media_files = indexed_media
    elif single_media:
        media_files = [single_media]
    else:
        media_files = []

    typed_files = []
    for f in media_files:
        try:
            kind = validate_uploaded_media_file(
                f, allow_kinds=('image', 'video', 'audio'),
            )
        except ValueError as exc:
            # Surface the validator's user-safe message verbatim; it
            # distinguishes "unsupported type" from "too large" from
            # "doesn't look like the format you claimed", which the
            # client can map to different toasts.
            return None, Response(
                {"error": f"{f.name}: {exc}"}, status=400,
            )
        typed_files.append((f, kind))
    return typed_files, None


def _resolve_conversation_for_send(request, conversation_id, target_user_id):
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

    if not target_user_id:
        return None, Response({"error": "target_user_id required"}, status=400)
    try:
        other_user = User.objects.get(id=target_user_id)
    except User.DoesNotExist:
        return None, Response({"error": "User not found"}, status=404)

    if BlockedUser.objects.between(request.user, other_user).exists():
        return None, Response({"error": "Not allowed"}, status=403)

    # M7: implicit-create path -- send_message called with target_user_id
    # instead of conversation_id. Mirror start_conversation's request-
    # throttle pre-check so a sender can't bypass the cap by going
    # straight to /send-message/ with target_user_id.
    will_be_request = not Follow.objects.filter(
        follower=other_user, following=request.user,
    ).exists()

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
        if will_be_request:
            err = check_request_throttle(request.user.id)
            if err:
                return None, Response({"error": err}, status=429)
        convo = Conversation.objects.create()
        convo.participants.add(request.user, other_user)
        ensure_participant_states(
            convo.id, request.user.id,
            [request.user.id, other_user.id],
        )
        if will_be_request:
            _count_request_creation(request.user.id)
    return convo, None


def _persist_message(convo, sender, text, typed_files, reply_to_obj):
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
@throttle_classes([SendMessageRateThrottle])
def send_message(request):
    conversation_id = request.data.get('conversation_id')
    target_user_id  = request.data.get('target_user_id')
    text            = (request.data.get('text') or '').strip()
    reply_to_id     = request.data.get('reply_to_id')

    # M10: cap text length before any other work. Same cap as the WS
    # path enforces in services/chat.py:create_chat_message. The 400
    # mentions the limit so the client can render a useful toast.
    # Run BEFORE _collect_typed_media so a too-long text doesn't burn
    # the per-file validation cost (which involves Pillow probes).
    if len(text) > MESSAGE_MAX_LEN:
        return Response(
            {"error": f"Message too long (max {MESSAGE_MAX_LEN} chars)."},
            status=400,
        )

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
        Conversation.objects.filter(id=convo.id).update(updated_at=timezone.now())

        # M7: implicit accept. If the sender's own participant state for
        # this conversation is is_accepted=False, sending a message
        # promotes the conversation into their main inbox. (Reading
        # never accepts; only sending does.) For the originator this
        # is a no-op (their state was True from creation).
        mark_accepted(convo.id, request.user.id)

    fields = _build_message_fields(request, message)
    _broadcast_new_message(convo, request, message, fields)

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

    if request.user not in convo.participants.all():
        return Response({"error": "Not allowed"}, status=403)

    for other_user in convo.participants.exclude(id=request.user.id):
        if BlockedUser.objects.between(request.user, other_user).exists():
            return Response({"error": "Conversation not found"}, status=404)

    qs = (
        Message.objects
        .filter(conversation=convo)
        .select_related(
            "sender", "sender__userprofile",
            "reply_to__sender",
            "shared_post", "shared_post__user", "shared_post__user__userprofile",
        )
        .prefetch_related("read_by", "reactions", "media_items", "shared_post__media")
        .order_by("-created_at")
    )

    if before_id is not None:
        qs = qs.filter(id__lt=before_id)

    messages = list(qs[:limit])
    messages.reverse()

    has_more = qs.filter(id__lt=messages[0].id).exists() if messages else False

    data = MessageSerializer(
        messages, many=True, context={'request': request, 'viewer': request.user}
    ).data

    return Response({
        "results":   data,
        "has_more":  has_more,
        "oldest_id": messages[0].id if messages else None,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def edit_message(request):
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
    message_id = request.data.get("message_id")
    if not message_id:
        return Response({"error": "message_id required."}, status=400)

    try:
        message = Message.objects.select_related("conversation").get(id=message_id)
    except Message.DoesNotExist:
        return Response({"error": "Message not found."}, status=404)

    if message.sender_id != request.user.id:
        return Response({"error": "Not your message."}, status=403)

    other_participants = message.conversation.participants.exclude(id=request.user.id)
    if other_participants.count() == 1:
        other = other_participants.first()
        if BlockedUser.objects.between(request.user, other).exists():
            return Response({"error": "Not allowed."}, status=403)

    if message.is_deleted:
        return Response({"status": "deleted", "message_id": message.id})

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
