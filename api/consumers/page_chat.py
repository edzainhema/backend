"""PageChatConsumer — receive-only WebSocket for a page's group chat (FE-2).

Mirrors :class:`ChatConsumer` but simpler: clients never send over this
socket — page-chat sends go through the REST ``send_page_chat_message``
view, which broadcasts the created message back to this group. The consumer
just authenticates, joins the per-page group, and relays new-message
frames, dropping messages from senders the viewer has blocked (either
direction) so the realtime stream matches ``list_page_chat_messages``'
block filtering.
"""
import json

from channels.db import database_sync_to_async

from ..services import chat as chat_service
from .base import BaseMessagingConsumer


class PageChatConsumer(BaseMessagingConsumer):
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

        if not await database_sync_to_async(chat_service.can_access_page_chat)(
            user.id, self.page_id
        ):
            await self.close(code=4403)
            return

        # Block set computed once at connect (mirrors the list view's two-way
        # block filter). It's slightly stale for the connection's lifetime —
        # the standard trade-off for a long-lived socket; a reconnect refreshes
        # it. Stored on the instance and consulted in broadcast().
        self.blocked_ids = await database_sync_to_async(
            chat_service.get_blocked_ids_for_user
        )(user.id)

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept_with_auth_subprotocol()

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
