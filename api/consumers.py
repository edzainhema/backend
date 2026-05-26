import json
from datetime import timedelta
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

from django.contrib.auth.models import User
from .models import BlockedUser, Conversation, Message, Device
from .utils import send_push_notification, push_to_user

# Kept in sync with the same constants in api/views/messaging.py so that
# the REST and WebSocket paths enforce identical edit rules. If you change
# one, change the other.
MESSAGE_EDIT_MAX_LEN = 4000
MESSAGE_EDIT_WINDOW = timedelta(minutes=15)

# WS-4: above this participant count, typing + read-receipt broadcasts (one
# delivery per member per event) are suppressed — they're a broadcast
# multiplier in big groups, and most UIs don't surface per-member "typing" /
# "seen" there anyway. DMs and small groups (<= threshold) are unaffected, and
# the read itself is still recorded (only the live broadcast is skipped).
LARGE_GROUP_BROADCAST_THRESHOLD = 20


def _dispatch_push_job(job):
    """Look up the recipient User and fire push_to_user.

    Wrapped in its own function so the receive() loop can hand it to
    sync_to_async in one call. Silently no-ops if the recipient was deleted
    between the message create and the push dispatch — there's no useful
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


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        raw_id = self.scope["url_route"]["kwargs"].get("conversation_id")
        try:
            self.conversation_id = int(raw_id)
        except (TypeError, ValueError):
            await self.close(code=4400)
            return
        self.room_group_name = f"chat_{self.conversation_id}"

        user = self.scope["user"]
        if not user or user.is_anonymous:
            await self.close(code=4401)
            return

        allowed = await self.user_in_conversation(user.id, self.conversation_id)
        if not allowed:
            await self.close(code=4403)
            return

        # WS-4: decide ONCE (not per event) whether this conversation is large
        # enough to suppress typing / read-receipt broadcasts. Participant count
        # changes rarely, so a slightly stale value is fine for a heuristic gate.
        participant_count = await self.get_participant_count(self.conversation_id)
        self.suppress_presence_broadcasts = (
            participant_count > LARGE_GROUP_BROADCAST_THRESHOLD
        )

        # Update last_seen on connect
        await self.update_last_seen(user.id)

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)

        # If the client offered the `auth.token` subprotocol (new WS auth
        # path), echo it back so the handshake completes successfully.
        # Falls back to a plain accept() for legacy querystring-token clients.
        auth_proto = None
        for proto in self.scope.get("subprotocols", []) or []:
            if proto == "auth.token":
                auth_proto = proto
                break
        if auth_proto:
            await self.accept(subprotocol=auth_proto)
        else:
            await self.accept()

    async def disconnect(self, close_code):
        user = self.scope.get("user")
        room = getattr(self, "room_group_name", None)
        if user and not user.is_anonymous:
            await self.update_last_seen(user.id)
            # Clear any stale "X is typing..." bubble on other clients --
            # without this, an abrupt disconnect leaves the indicator up
            # for the full 4-second client-side timeout.
            if room:
                try:
                    await self.channel_layer.group_send(room, {
                        "type": "broadcast",
                        "payload": {
                            "type":      "typing",
                            "user_id":   user.id,
                            "username":  user.username,
                            "is_typing": False,
                        },
                    })
                except Exception:
                    pass
        if room:
            await self.channel_layer.group_discard(room, self.channel_name)

    async def receive(self, text_data):
        user = self.scope["user"]
        try:
            data = json.loads(text_data)
        except (ValueError, TypeError):
            # Ignore malformed frames instead of crashing the socket.
            return
        if not isinstance(data, dict):
            return
        event = data.get("type")

        # Update last_seen on any activity
        await self.update_last_seen(user.id)

        # 1) SEND MESSAGE (text only -- media always comes via REST)
        if event == "message.send":
            text = (data.get("text") or "").strip()
            if not text:
                return

            reply_to_id = data.get("reply_to_id")
            result = await self.create_message(
                self.conversation_id, user.id, text, reply_to_id
            )
            # create_message returns None if the conversation or sender
            # row was deleted between handshake and this call.
            if result is None:
                return
            msg, push_jobs = result

            await self.channel_layer.group_send(self.room_group_name, {
                "type": "broadcast",
                "payload": {
                    "type": "message.new",
                    "message": msg,
                }
            })

            # Hand the per-recipient push fan-out to a Celery worker (WS-3) so
            # the receive loop isn't blocked by N sequential FCM round trips.
            # The task does ONE Device query for the whole batch and still sends
            # each recipient their own for_user_id (multi-account routing). If
            # the queue is unavailable (celery not installed / broker down), fall
            # back to the inline per-recipient dispatch so pushes still go out —
            # on the threadpool, never blocking message delivery.
            if push_jobs and not await self._enqueue_push_fanout(push_jobs):
                for job in push_jobs:
                    try:
                        await sync_to_async(_dispatch_push_job, thread_sensitive=False)(job)
                    except Exception:
                        pass

        # 2) TYPING INDICATOR
        elif event == "typing":
            # WS-4: skip the typing fan-out entirely in large groups.
            if self.suppress_presence_broadcasts:
                return
            is_typing = bool(data.get("is_typing"))
            await self.channel_layer.group_send(self.room_group_name, {
                "type": "broadcast",
                "payload": {
                    "type": "typing",
                    "user_id": user.id,
                    "username": user.username,
                    "is_typing": is_typing,
                }
            })

        # 3) READ RECEIPTS
        elif event == "message.read":
            message_id = data.get("message_id")
            if not message_id:
                return

            updated = await self.mark_read(message_id, user.id, self.conversation_id)
            if not updated:
                return

            # WS-4: the read is recorded above (read_by → unread counts stay
            # correct); only the live "seen" broadcast is skipped in large
            # groups, where per-member receipts are noise + a fan-out multiplier.
            if self.suppress_presence_broadcasts:
                return

            await self.channel_layer.group_send(self.room_group_name, {
                "type": "broadcast",
                "payload": {
                    "type": "message.read",
                    "message_id": int(message_id),
                    "user_id": user.id,
                    "username": user.username,
                }
            })

        # 4) MESSAGE DELETION (soft delete)
        elif event == "message.delete":
            message_id = data.get("message_id")
            if not message_id:
                return

            ok = await self.soft_delete_message(message_id, user.id, self.conversation_id)
            if not ok:
                return

            await self.channel_layer.group_send(self.room_group_name, {
                "type": "broadcast",
                "payload": {
                    "type": "message.deleted",
                    "message_id": int(message_id),
                    "deleted_by": user.id,
                }
            })

        # 5) MESSAGE EDIT
        elif event == "message.edit":
            message_id = data.get("message_id")
            new_text = (data.get("text") or "").strip()
            if not message_id or not new_text:
                return

            ok = await self.edit_message(message_id, user.id, self.conversation_id, new_text)
            if not ok:
                return

            await self.channel_layer.group_send(self.room_group_name, {
                "type": "broadcast",
                "payload": {
                    "type": "message.edited",
                    "message_id": int(message_id),
                    "text": new_text,
                    "edited_by": user.id,
                    "last_edited_at": timezone.now().isoformat(),
                }
            })

    async def broadcast(self, event):
        await self.send(text_data=json.dumps(event["payload"]))

    # Called by send_message REST view after saving a media message
    async def chat_media_message(self, event):
        """Broadcast a media message created by the REST endpoint to all WS clients."""
        await self.send(text_data=json.dumps(event["payload"]))

    async def _enqueue_push_fanout(self, push_jobs) -> bool:
        """Enqueue ONE push fan-out task for a message's recipients (WS-3).

        Returns True if it was handed to the queue (or ran eagerly), False if the
        queue is unavailable (celery not installed / broker down) so the caller
        can fall back to inline dispatch. Every recipient of one message shares
        the same title/body/extra_data, so we pass those once plus the recipient
        id list. Enqueued via sync_to_async so the broker publish (or an eager
        in-process run) never blocks the event loop.
        """
        try:
            from .tasks import dispatch_push_to_many
        except Exception:
            return False
        first = push_jobs[0]
        recipient_ids = [j["recipient_id"] for j in push_jobs]
        try:
            await sync_to_async(
                dispatch_push_to_many.delay, thread_sensitive=False
            )(recipient_ids, first["title"], first["body"], first.get("extra_data"))
            return True
        except Exception:
            return False

    # ----------------- DB helpers -----------------

    @database_sync_to_async
    def user_in_conversation(self, user_id: int, conversation_id: int) -> bool:
        return Conversation.objects.filter(
            id=conversation_id, participants__id=user_id
        ).exists()

    @database_sync_to_async
    def get_participant_count(self, conversation_id: int) -> int:
        # One COUNT at connect; drives WS-4 presence-broadcast suppression.
        try:
            convo = Conversation.objects.get(id=conversation_id)
        except Conversation.DoesNotExist:
            return 0
        return convo.participants.count()

    @database_sync_to_async
    def update_last_seen(self, user_id: int) -> None:
        # Throttled (WS-2): at most one last_seen DB write per ~45s per user,
        # not one per inbound frame. Runs in the DB threadpool via the decorator.
        from .utils import touch_last_seen
        touch_last_seen(user_id)

    @database_sync_to_async
    def create_message(self, conversation_id, user_id, text, reply_to_id=None):
        """
        Creates the Message row, bumps Conversation.updated_at, and returns
        a (message_dict, push_jobs) tuple.

        push_jobs is a list of dicts, one per recipient:
            {"recipient_id": int, "title": str, "body": str, "extra_data": dict}
        The async caller fans these out via sync_to_async + push_to_user so
        the FCM round-trips don't run on the DB-sync threadpool. Each push
        carries its own for_user_id (added by push_to_user) so a phone with
        multiple accounts registered routes the notification correctly.

        None is returned if the conversation or sender row was deleted
        between handshake and now (callers must skip the broadcast).
        """
        # Defensive lookups -- a delete on either side between WS handshake
        # and this call must NOT 500 the receive loop.
        try:
            convo = Conversation.objects.prefetch_related("participants").get(id=conversation_id)
        except Conversation.DoesNotExist:
            return None
        try:
            sender = User.objects.select_related("userprofile").get(id=user_id)
        except User.DoesNotExist:
            return None

        # Mirror the block check enforced by the REST send_message endpoint
        # (see views/messaging.py): if this is a 1:1 DM and either party has
        # blocked the other, refuse to create the message. Without this the
        # socket lets blocked users keep exchanging messages even though the
        # REST path returns 403 for the identical action.
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
                # Resolve a usable media_url for the reply preview. The old
                # code hardcoded None even when the parent message had media.
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

        # Build a per-recipient push job list here (where we already have a
        # DB session) but DO NOT fire any FCM calls from inside this sync
        # thread -- the receive handler dispatches them via sync_to_async
        # after we return. Each recipient gets their own push so the device
        # can route it to the correct in-app account when multiple accounts
        # are registered for push on the same phone.
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

    @database_sync_to_async
    def mark_read(self, message_id: int, user_id: int, conversation_id: int) -> bool:
        """Returns True only when this is a NEW read, so callers don't
        broadcast redundant `message.read` events. Also refuses to record
        the sender as a reader of their own message."""
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

    @database_sync_to_async
    def soft_delete_message(self, message_id: int, user_id: int, conversation_id: int) -> bool:
        try:
            m = Message.objects.get(id=message_id, conversation_id=conversation_id)
        except Message.DoesNotExist:
            return False

        if m.sender_id != user_id:
            return False

        # Shared with the REST delete_message view: soft-delete the row AND
        # hard-delete the media blobs (legacy `media` + MessageMedia rows) so a
        # deleted photo/video isn't left downloadable and storage doesn't
        # accumulate orphans (M4). Idempotent, so a repeat delete is a no-op.
        m.soft_delete()
        return True

    @database_sync_to_async
    def edit_message(self, message_id: int, user_id: int, conversation_id: int, new_text: str) -> bool:
        """Only the sender can edit their own non-deleted message.

        Mirrors the constraints enforced by the REST `edit_message` view
        (views/messaging.py): edits must be within MESSAGE_EDIT_WINDOW of
        the original send, and the new text must not exceed
        MESSAGE_EDIT_MAX_LEN characters. Without these the socket path lets
        a sender edit messages from any time, with arbitrarily long text,
        while the REST path rejects the same action.
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


class PageChatConsumer(AsyncWebsocketConsumer):
    """Receive-only WebSocket for a page's group chat (FE-2).

    Mirrors ChatConsumer but simpler: clients never send over this socket —
    page-chat sends go through the REST send_page_chat_message view, which
    broadcasts the created message back to this group. The consumer just
    authenticates, joins the per-page group, and relays new-message frames,
    dropping messages from senders the viewer has blocked (either direction)
    so the realtime stream matches list_page_chat_messages' block filtering.
    """

    async def connect(self):
        raw_id = self.scope["url_route"]["kwargs"].get("page_id")
        try:
            self.page_id = int(raw_id)
        except (TypeError, ValueError):
            await self.close(code=4400)
            return
        self.group_name = f"pagechat_{self.page_id}"

        user = self.scope["user"]
        if not user or user.is_anonymous:
            await self.close(code=4401)
            return

        if not await self.can_access(user.id, self.page_id):
            await self.close(code=4403)
            return

        # Block set computed once at connect (mirrors the list view's two-way
        # block filter). It's slightly stale for the connection's lifetime —
        # the standard trade-off for a long-lived socket; a reconnect refreshes
        # it. Stored on the instance and consulted in broadcast().
        self.blocked_ids = await self.get_blocked_ids(user.id)

        await self.channel_layer.group_add(self.group_name, self.channel_name)

        # Echo the auth.token subprotocol back (matches ChatConsumer) so the
        # subprotocol-auth handshake completes; fall back to a plain accept.
        auth_proto = None
        for proto in self.scope.get("subprotocols", []) or []:
            if proto == "auth.token":
                auth_proto = proto
                break
        if auth_proto:
            await self.accept(subprotocol=auth_proto)
        else:
            await self.accept()

    async def disconnect(self, close_code):
        group = getattr(self, "group_name", None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive(self, text_data):
        # Clients never send over this socket; ignore any inbound frames.
        return

    async def broadcast(self, event):
        payload = event.get("payload") or {}
        msg = payload.get("message") or {}
        sender = msg.get("sender") or {}
        sid = sender.get("id")
        # Drop messages from blocked senders (either direction), matching
        # list_page_chat_messages so the realtime view never shows a message
        # the REST list would have filtered out.
        if sid is not None and sid in getattr(self, "blocked_ids", set()):
            return
        await self.send(text_data=json.dumps(payload))

    # ----------------- DB helpers -----------------

    @database_sync_to_async
    def can_access(self, user_id: int, page_id: int) -> bool:
        from .models import Page, PageFollow
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

    @database_sync_to_async
    def get_blocked_ids(self, user_id: int) -> set:
        ids = set()
        for u, b in BlockedUser.objects.involving(user_id).values_list(
            "user_id", "blocked_user_id"
        ):
            ids.add(u)
            ids.add(b)
        ids.discard(user_id)
        return ids
