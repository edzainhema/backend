"""
Chat consumer business logic.

The WebSocket ``ChatConsumer`` / ``PageChatConsumer`` in ``api/consumers.py``
are deliberately thin: they handle WS protocol — accept the connection, gate
auth & membership, join the channel-layer group, dispatch inbound frames to
broadcasts. Everything that touches the DB or computes per-message business
rules (message create, mark-read, edit, soft-delete, push fan-out, page-chat
membership / block filtering) lives here, so the consumer stays a thin
dispatcher — mirroring the views/ → services/ split already in place for the
REST message endpoints (see ``api/views/messaging/``).

All functions in this module are synchronous: the consumer wraps each call
in ``database_sync_to_async`` (or ``sync_to_async`` for the non-DB push
helpers) at the call site.
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from ..models import BlockedUser, Conversation, Message
from .push import push_to_user


# Edit constraints — kept identical to the REST message-edit view
# (``api/views/messaging/messages.py``), which imports these names from this
# module. If you tighten one, you tighten both.
MESSAGE_EDIT_MAX_LEN = 4000
MESSAGE_EDIT_WINDOW = timedelta(minutes=15)

# WS-4: above this participant count, typing + read-receipt broadcasts are
# suppressed (one delivery per member per event would otherwise be a fan-out
# multiplier in big groups, and most UIs don't surface per-member typing/seen
# there). DMs and small groups (<= threshold) are unaffected; the read itself
# is still recorded — only the live broadcast is skipped.
LARGE_GROUP_BROADCAST_THRESHOLD = 20


# ----------------------------------------------------------------------
# Membership / participant helpers
# ----------------------------------------------------------------------

def is_user_in_conversation(user_id: int, conversation_id: int) -> bool:
    return Conversation.objects.filter(
        id=conversation_id, participants__id=user_id
    ).exists()


def get_conversation_participant_count(conversation_id: int) -> int:
    # One COUNT at connect; drives WS-4 presence-broadcast suppression.
    try:
        convo = Conversation.objects.get(id=conversation_id)
    except Conversation.DoesNotExist:
        return 0
    return convo.participants.count()


# ----------------------------------------------------------------------
# Message create / mark-read / edit / delete
# ----------------------------------------------------------------------

def create_chat_message(conversation_id, user_id, text, reply_to_id=None):
    """
    Create the ``Message`` row, bump ``Conversation.updated_at``, and return
    a ``(message_dict, push_jobs)`` tuple.

    ``push_jobs`` is a list of dicts, one per recipient::

        {"recipient_id": int, "title": str, "body": str, "extra_data": dict}

    The async caller fans these out via ``sync_to_async`` + ``push_to_user``
    so the FCM round-trips don't run on the DB-sync threadpool. Each push
    carries its own ``for_user_id`` (added by ``push_to_user``) so a phone
    with multiple accounts registered routes the notification correctly.

    Returns ``None`` if the conversation or sender row was deleted between
    the WS handshake and now (callers must skip the broadcast).
    """
    # Defensive lookups — a delete on either side between WS handshake and
    # this call must NOT 500 the receive loop.
    try:
        convo = Conversation.objects.prefetch_related("participants").get(id=conversation_id)
    except Conversation.DoesNotExist:
        return None
    try:
        sender = User.objects.select_related("userprofile").get(id=user_id)
    except User.DoesNotExist:
        return None

    # Mirror the block check enforced by the REST send_message endpoint
    # (views/messaging.py): if this is a 1:1 DM and either party has blocked
    # the other, refuse to create the message. Without this the socket lets
    # blocked users keep exchanging messages even though the REST path
    # returns 403 for the identical action.
    participants = list(convo.participants.all())
    if len(participants) == 2:
        other_user = next((p for p in participants if p.id != user_id), None)
        if other_user is not None and BlockedUser.objects.between(
            user_id, other_user
        ).exists():
            return None

    reply_to_obj = None
    reply_to_data = None
    if reply_to_id:
        try:
            reply_to_obj = (
                Message.objects
                .select_related("sender")
                .prefetch_related("media_items")
                .get(id=reply_to_id, conversation_id=conversation_id)
            )
            r = reply_to_obj
            # Resolve a usable media_url for the reply preview. The old code
            # hardcoded None even when the parent message had media.
            reply_media_url = None
            reply_media_type = r.media_type
            if not r.is_deleted:
                site_url = getattr(settings, "SITE_URL", "").rstrip("/")
                if r.media:
                    reply_media_url = f"{site_url}{r.media.url}"
                if not reply_media_url:
                    first_item = next(iter(r.media_items.all()), None)
                    if first_item:
                        reply_media_url = f"{site_url}{first_item.file.url}"
                        reply_media_type = reply_media_type or first_item.media_type
            reply_to_data = {
                "id":         r.id,
                "sender_id":  r.sender_id,
                "sender":     r.sender.username,
                "text":       "" if r.is_deleted else r.text,
                "media_url":  reply_media_url,
                "media_type": reply_media_type,
                "is_deleted": r.is_deleted,
            }
        except Message.DoesNotExist:
            pass

    m = Message.objects.create(
        conversation=convo,
        sender=sender,
        text=text,
        reply_to=reply_to_obj,
    )

    # Bump conversation updated_at so list re-sorts
    Conversation.objects.filter(id=conversation_id).update(updated_at=timezone.now())

    # Build a per-recipient push job list here (where we already have a DB
    # session) but DO NOT fire any FCM calls from inside this sync thread —
    # the receive handler dispatches them via sync_to_async after we return.
    # Each recipient gets their own push so the device can route it to the
    # correct in-app account when multiple accounts are registered for push
    # on the same phone.
    push_jobs = []
    try:
        recipient_ids = list(
            convo.participants.exclude(id=sender.id).values_list("id", flat=True)
        )
        body = text if text else "New message"
        extra_data = {
            "type": "message",
            "conversation_id": conversation_id,
            "sender_id": sender.id,
        }
        for rid in recipient_ids:
            push_jobs.append({
                "recipient_id": rid,
                "title": sender.username,
                "body": body,
                "extra_data": extra_data,
            })
    except Exception:
        push_jobs = []

    sender_avatar = None
    profile = getattr(sender, "userprofile", None)
    if profile and profile.avatar:
        site_url = getattr(settings, "SITE_URL", "").rstrip("/")
        sender_avatar = f"{site_url}{profile.avatar.url}"

    message_dict = {
        "id":              m.id,
        "conversation_id": conversation_id,
        "sender_id":       sender.id,
        "sender":          sender.username,
        "sender_avatar":   sender_avatar,
        "text":            m.text,
        "created_at":      m.created_at.isoformat(),
        "is_deleted":      m.is_deleted,
        "is_edited":       False,
        "last_edited_at":  None,
        "read_by":         [],
        "media_url":       None,
        "media_type":      None,
        "media_items":     [],
        "reply_to":        reply_to_data,
    }
    return message_dict, push_jobs


def mark_message_read(message_id: int, user_id: int, conversation_id: int) -> bool:
    """Returns True only when this is a NEW read, so callers don't broadcast
    redundant ``message.read`` events. Also refuses to record the sender as a
    reader of their own message."""
    try:
        m = Message.objects.only("id", "sender_id").get(
            id=message_id, conversation_id=conversation_id
        )
    except Message.DoesNotExist:
        return False
    if m.sender_id == user_id:
        return False
    if m.read_by.filter(id=user_id).exists():
        return False
    m.read_by.add(user_id)
    return True


def soft_delete_chat_message(message_id: int, user_id: int, conversation_id: int) -> bool:
    try:
        m = Message.objects.get(id=message_id, conversation_id=conversation_id)
    except Message.DoesNotExist:
        return False

    if m.sender_id != user_id:
        return False

    # Shared with the REST delete_message view: soft-delete the row AND
    # hard-delete the media blobs (legacy ``media`` + MessageMedia rows) so a
    # deleted photo/video isn't left downloadable and storage doesn't
    # accumulate orphans (M4). Idempotent — a repeat delete is a no-op.
    m.soft_delete()
    return True


def edit_chat_message(message_id: int, user_id: int, conversation_id: int, new_text: str) -> bool:
    """Only the sender can edit their own non-deleted message.

    Mirrors the constraints enforced by the REST ``edit_message`` view
    (views/messaging.py): edits must be within ``MESSAGE_EDIT_WINDOW`` of the
    original send, and the new text must not exceed ``MESSAGE_EDIT_MAX_LEN``
    characters. Without these the socket path would let a sender edit
    messages from any time, with arbitrarily long text, while the REST path
    rejects the same action.
    """
    if len(new_text) > MESSAGE_EDIT_MAX_LEN:
        return False

    try:
        m = Message.objects.get(id=message_id, conversation_id=conversation_id)
    except Message.DoesNotExist:
        return False

    if m.sender_id != user_id or m.is_deleted:
        return False

    if timezone.now() - m.created_at > MESSAGE_EDIT_WINDOW:
        return False

    m.text = new_text
    m.is_edited = True
    m.last_edited_at = timezone.now()
    m.save(update_fields=["text", "is_edited", "last_edited_at"])
    return True


# ----------------------------------------------------------------------
# Push fan-out helpers
# ----------------------------------------------------------------------

def dispatch_push_job(job):
    """Look up the recipient ``User`` and fire ``push_to_user``.

    Wrapped in its own function so the receive() loop can hand it to
    ``sync_to_async`` in one call. Silently no-ops if the recipient was
    deleted between message create and push dispatch — there's no useful
    follow-up the WebSocket consumer could take.
    """
    try:
        recipient = User.objects.get(id=job["recipient_id"])
    except User.DoesNotExist:
        return
    push_to_user(
        recipient,
        title=job["title"],
        body=job["body"],
        extra_data=job.get("extra_data"),
    )


def enqueue_push_fanout(push_jobs) -> bool:
    """Enqueue ONE push fan-out task for a message's recipients (WS-3).

    Returns True if the job was handed to the queue (or ran eagerly), False
    if the queue is unavailable (celery not installed / broker down) so the
    caller can fall back to inline dispatch. Every recipient of one message
    shares the same title/body/extra_data, so we pass those once plus the
    recipient id list.

    Synchronous: imports the celery task and calls ``.delay`` (a quick AMQP
    publish on a healthy broker, but can still block — hence the caller
    wraps it in ``sync_to_async``).
    """
    if not push_jobs:
        return True
    try:
        from ..tasks import dispatch_push_to_many
    except Exception:
        return False
    first = push_jobs[0]
    recipient_ids = [j["recipient_id"] for j in push_jobs]
    try:
        dispatch_push_to_many.delay(
            recipient_ids, first["title"], first["body"], first.get("extra_data")
        )
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------
# Page-chat membership / block helpers
# ----------------------------------------------------------------------

def can_access_page_chat(user_id: int, page_id: int) -> bool:
    # Lazy import — Page/PageFollow live in the same models package, but
    # importing them at module load isn't necessary and keeps the import
    # graph slimmer for the rest of the chat-service surface.
    from ..models import Page, PageFollow
    try:
        page = Page.objects.only("id", "owner_id", "chat_enabled").get(id=page_id)
    except Page.DoesNotExist:
        return False
    # The owner can read even while chat is off (matches list_page_chat_messages).
    if page.owner_id == user_id:
        return True
    if not page.chat_enabled:
        return False
    return PageFollow.objects.filter(user_id=user_id, page_id=page_id).exists()


def get_blocked_ids_for_user(user_id: int) -> set:
    ids = set()
    for u, b in BlockedUser.objects.involving(user_id).values_list(
        "user_id", "blocked_user_id"
    ):
        ids.add(u)
        ids.add(b)
    ids.discard(user_id)
    return ids
