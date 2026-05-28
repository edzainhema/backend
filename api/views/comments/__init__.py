"""Comment views, split by concern. Re-exported so `from ..comments import X`
and `views.X` keep resolving unchanged."""

from .read import (
    get_comments,
)
from .create import (
    create_comment,
)
from .actions import (
    delete_comment,
    toggle_comment_like,
)

__all__ = [
    "get_comments",
    "create_comment",
    "delete_comment",
    "toggle_comment_like",
]
