"""WebSocket consumers for the messaging layer.

Split under audit N4 from the former flat ``api/consumers.py``, mirroring
the ``views/`` → ``views/<domain>/`` packaging pattern already in place for
the HTTP layer. External callers (``api.routing``, tests, etc.) continue to
import the consumer classes from this package unchanged::

    from api.consumers import ChatConsumer, PageChatConsumer

Layout:

* ``base.py`` — :class:`BaseMessagingConsumer`, with the shared
  ``accept_with_auth_subprotocol`` helper (echoes the ``auth.token``
  subprotocol when the new-WS-auth client offers it).
* ``chat.py`` — :class:`ChatConsumer` (1:1 / group DM socket).
* ``page_chat.py`` — :class:`PageChatConsumer` (receive-only page-group
  socket).

DB-touching / business-rule logic for both consumers lives in
:mod:`api.services.chat` (extracted under audit SC3); the classes here are
pure WS-protocol dispatchers.
"""
from .chat import ChatConsumer
from .page_chat import PageChatConsumer

__all__ = ["ChatConsumer", "PageChatConsumer"]
