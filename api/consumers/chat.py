"""ChatConsumer — the 1:1 / group chat WebSocket.

Authenticates via JWT (see ``api.jwt_middleware``), joins the
``chat_<conversation_id>`` group, and dispatches each inbound frame to the
right helper in ``api.services.chat``. The consumer itself stays a thin
WS-protocol dispatcher — all DB-touching / business-rule logic lives in
``api.services.chat`` (SC3).
"""
import json

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from django.utils import timezone

from ..services import chat as chat_service
from ..services.presence import touch_last_seen
from .base import BaseMessagingConsumer


class ChatConsumer(BaseMessagingConsumer):
    """1:1 / group chat WebSocket."""

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

        allowed = await database_sync_to_async(chat_service.is_user_in_conversation)(
            user.id, self.conversation_id
        )
        if not allowed:
            await self.close(code=4403)
            return

        # WS-4: decide ONCE (not per event) whether this conversation is large
        # enough to suppress typing / read-receipt broadcasts. Participant
        # count changes rarely, so a slightly stale value is fine for a
        # heuristic gate.
        participant_count = await database_sync_to_async(
            chat_service.get_conversation_participant_count
        )(self.conversation_id)
        self.suppress_presence_broadcasts = (
            participant_count > chat_service.LARGE_GROUP_BROADCAST_THRESHOLD
        )

        # Update last_seen on connect.
        await database_sync_to_async(touch_last_seen)(user.id)

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept_with_auth_subprotocol()

    async def disconnect(self, close_code):
        user = self.scope.get("user")
        room = getattr(self, "room_group_name", None)
        if user and not user.is_anonymous:
            await database_sync_to_async(touch_last_seen)(user.id)
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

        # Update last_seen on any activity.
        await database_sync_to_async(touch_last_seen)(user.id)

        # 1) SEND MESSAGE (text only -- media always comes via REST)
        if event == "message.send":
            text = (data.get("text") or "").strip()
            if not text:
                return

            reply_to_id = data.get("reply_to_id")
            result = await database_sync_to_async(chat_service.create_chat_message)(
                self.conversation_id, user.id, text, reply_to_id
            )
            # create_chat_message returns None if the conversation or sender
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

            # Hand the per-recipient push fan-out to a Celery worker (WS-3)
            # so the receive loop isn't blocked by N sequential FCM round
            # trips. If the queue is unavailable (celery not installed /
            # broker down), fall back to the inline per-recipient dispatch
            # so pushes still go out — on the threadpool, never blocking
            # message delivery.
            if push_jobs:
                queued = await sync_to_async(
                    chat_service.enqueue_push_fanout, thread_sensitive=False
                )(push_jobs)
                if not queued:
                    for job in push_jobs:
                        try:
                            await sync_to_async(
                                chat_service.dispatch_push_job,
                                thread_sensitive=False,
                            )(job)
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

            updated = await database_sync_to_async(chat_service.mark_message_read)(
                message_id, user.id, self.conversation_id
            )
            if not updated:
                return

            # WS-4: the read is recorded above (read_by → unread counts stay
            # correct); only the live "seen" broadcast is skipped in large
            # groups, where per-member receipts are noise + a fan-out
            # multiplier.
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

            ok = await database_sync_to_async(chat_service.soft_delete_chat_message)(
                message_id, user.id, self.conversation_id
            )
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

            ok = await database_sync_to_async(chat_service.edit_chat_message)(
                message_id, user.id, self.conversation_id, new_text
            )
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
