"""feed_helpers split into a package along its natural seams (audit SC1).
Public names are re-exported here so existing
`from ..services.feed_helpers import X` callers resolve unchanged."""

from .counts import (
    likes_count_subquery,
    comments_count_subquery,
    saves_count_subquery,
)
from .visibility import (
    can_user_post_on_page,
    get_muted_page_ids,
    post_visibility_q,
    viewer_can_see_post,
)
from .social import (
    get_very_close_friend_ids,
    get_friend_ids,
    get_social_sets,
    get_social_overlap_score,
)
from .context import (
    _build_feed_context_uncached,
    build_feed_context,
)
from .render import (
    serialize_post,
    merge_feed,
    recency_decay,
)
from .feeds import (
    get_followed_feed,
    get_suggested_feed,
)

__all__ = [
    "likes_count_subquery", "comments_count_subquery", "saves_count_subquery",
    "can_user_post_on_page", "get_muted_page_ids", "post_visibility_q",
    "viewer_can_see_post",
    "get_very_close_friend_ids", "get_friend_ids", "get_social_sets",
    "get_social_overlap_score",
    "_build_feed_context_uncached", "build_feed_context",
    "serialize_post", "merge_feed", "recency_decay",
    "get_followed_feed", "get_suggested_feed",
]
