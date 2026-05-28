"""Shared base for messaging WebSocket consumers.

Both ``ChatConsumer`` (``chat.py``) and ``PageChatConsumer``
(``page_chat.py``) authenticate via the ``auth.token`` subprotocol (see
``api.jwt_middleware``) and need to echo that subprotocol back when
accepting the connection so the handshake completes. That accept logic is
the one piece of WS-protocol code shared between the two consumers;
everything else (group routing, broadcast filtering, receive dispatch) is
different enough that a shared base for those would be premature
abstraction.
"""
from channels.generic.websocket import AsyncWebsocketConsumer


class BaseMessagingConsumer(AsyncWebsocketConsumer):
    """``AsyncWebsocketConsumer`` plus a shared subprotocol-echo accept helper."""

    async def accept_with_auth_subprotocol(self) -> None:
        """Accept the connection, echoing ``auth.token`` if the client offered it.

        Channels requires the server to choose ONE of the offered
        subprotocols; without echoing ``auth.token`` back the client receives
        an empty ``Sec-WebSocket-Protocol`` header and the new-WS-auth
        handshake fails. Falls back to a plain ``accept()`` for legacy
        querystring-token clients that don't offer a subprotocol.
        """
        for proto in self.scope.get("subprotocols", []) or []:
            if proto == "auth.token":
                await self.accept(subprotocol=proto)
                return
        await self.accept()
