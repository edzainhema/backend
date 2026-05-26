"""Messaging views, split by concern. Re-exported so `from ..messaging import X`
and `views.X` keep resolving unchanged."""

from .conversations import (
    start_conversation,
    start_group_conversation,
    list_conversations,
    delete_conversation,
    rename_conversation,
    search_message_users,
)
from .messages import (
    _push_new_message,
    send_message,
    get_messages,
    edit_message,
    delete_message,
)
from .reactions import (
    _reaction_summary,
    react_to_message,
)

__all__ = [
    "_reaction_summary",
    "_push_new_message",
    "start_conversation",
    "start_group_conversation",
    "send_message",
    "get_messages",
    "list_conversations",
    "delete_conversation",
    "react_to_message",
    "edit_message",
    "delete_message",
    "rename_conversation",
    "search_message_users",
]
