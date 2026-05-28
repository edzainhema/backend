"""Search views, split by concern. Re-exported so `from ..search import X`
and `views.X` keep resolving unchanged."""

from .combined import (
    search,
)
from .history import (
    search_history,
)
from .posts import (
    search_posts,
)
from .pages import (
    search_pages,
)

__all__ = [
    "search",
    "search_history",
    "search_posts",
    "search_pages",
]
