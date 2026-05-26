"""Page views, split by concern. Re-exported here so `from ..pages import X`
and `views.X` (via api.views.__init__) keep resolving unchanged."""

from .core import (
    create_page,
    get_page_detail,
    toggle_page_follow,
    list_pages,
    update_page_avatar,
    toggle_page_mute,
    toggle_page_pin,
    update_page_settings,
)
from .invites import (
    list_sent_page_invites,
    search_users_for_page_invite,
    invite_to_page,
    cancel_page_invite,
    respond_to_page_invite,
)
from .posters import (
    get_page_posters,
    toggle_page_poster,
    search_page_posters,
)
from .followers import (
    approve_page_follow_request,
    reject_page_follow_request,
    get_page_followers,
    remove_page_follower,
)
from .location import (
    places_autocomplete,
    place_details,
    set_page_location,
)

__all__ = [
    "approve_page_follow_request",
    "reject_page_follow_request",
    "create_page",
    "get_page_detail",
    "toggle_page_follow",
    "list_pages",
    "update_page_avatar",
    "list_sent_page_invites",
    "search_users_for_page_invite",
    "invite_to_page",
    "cancel_page_invite",
    "respond_to_page_invite",
    "toggle_page_mute",
    "toggle_page_pin",
    "update_page_settings",
    "get_page_posters",
    "toggle_page_poster",
    "search_page_posters",
    "get_page_followers",
    "remove_page_follower",
    "places_autocomplete",
    "place_details",
    "set_page_location",
]
