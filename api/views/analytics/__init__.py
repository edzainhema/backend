"""Analytics / activity-logging views, split by concern. Re-exported so
`from ..analytics import X` and `views.X` keep resolving unchanged."""

from .posts import (
    log_post_view,
    log_post_dwell,
    log_post_share,
)
from .watch import (
    log_video_watch,
    log_reel_watch,
)
from .interactions import (
    log_profile_visit,
    log_comment_scroll,
    log_search_click,
    log_tab_view,
    log_hashtag_engagement,
)

__all__ = [
    "log_profile_visit",
    "log_video_watch",
    "log_post_view",
    "log_post_dwell",
    "log_comment_scroll",
    "log_reel_watch",
    "log_search_click",
    "log_hashtag_engagement",
    "log_tab_view",
    "log_post_share",
]
