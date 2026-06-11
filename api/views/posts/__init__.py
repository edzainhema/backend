"""Post views, split by concern. Re-exported so `from ..posts import X`
and `views.X` keep resolving unchanged."""

from .create import (
    create_post,
)
from .status import (
    post_hls_status,
)
from .engagement import (
    toggle_post_like,
    toggle_post_save,
    not_interested,
)
from .saved import (
    saved_posts,
)
from .lifecycle import (
    delete_post,
    toggle_post_public,
    list_trashed_posts,
    purge_post,
    restore_post,
)

__all__ = [
    "create_post",
    "post_hls_status",
    "toggle_post_like",
    "toggle_post_save",
    "not_interested",
    "saved_posts",
    "delete_post",
    "toggle_post_public",
    "list_trashed_posts",
    "purge_post",
    "restore_post",
]
