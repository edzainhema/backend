"""
Re-export shim for the legacy `api.views` module path.

This package replaces the old single-file views.py. Every URL route in
urls.py and every legacy `from api.views import X` call keeps working
because the relevant names are re-exported here.
"""

# View functions, grouped by module
from .analytics import (
    log_profile_visit,
    log_video_watch,
    log_post_view,
    log_post_dwell,
    log_comment_scroll,
    log_reel_watch,
    log_search_click,
    log_hashtag_engagement,
    log_tab_view,
    log_post_share,
)
from .activity_batch import log_activity_batch
from .auth import (
    register_user,
    login_user,
    social_auth,
)
from .comments import (
    get_comments,
    create_comment,
    delete_comment,
    toggle_comment_like,
)
from .devices import (
    register_device,
    unregister_device,
    update_user_location,
)
from .feed import (
    home_feed,
    explore_feed,
)
from .follow import (
    toggle_follow,
    approve_follow_request,
    reject_follow_request,
    list_my_followers,
    remove_my_follower,
)
from .memories import (
    toggle_page_memory,
    get_user_memories,
)
from .messaging import (
    _reaction_summary,
    _push_new_message,
    start_conversation,
    start_group_conversation,
    send_message,
    get_messages,
    list_conversations,
    delete_conversation,
    react_to_message,
    edit_message,
    delete_message,
    rename_conversation,
    search_message_users,
)
from .notifications import (
    list_notifications,
    mark_notification_read,
    mark_all_notifications_read,
    unread_notifications_count,
)
from .page_chat import (
    _page_chat_member_or_403,
    _serialize_page_chat_message,
    toggle_page_chat,
    list_page_chat_messages,
    send_page_chat_message,
)
from .pages import (
    approve_page_follow_request,
    reject_page_follow_request,
    create_page,
    get_page_detail,
    toggle_page_follow,
    list_pages,
    update_page_avatar,
    list_sent_page_invites,
    search_users_for_page_invite,
    invite_to_page,
    cancel_page_invite,
    respond_to_page_invite,
    toggle_page_mute,
    toggle_page_pin,
    update_page_settings,
    get_page_posters,
    toggle_page_poster,
    search_page_posters,
    get_page_followers,
    remove_page_follower,
    places_autocomplete,
    place_details,
    set_page_location,
)
from .posts import (
    create_post,
    toggle_post_like,
    toggle_post_save,
    not_interested,
    saved_posts,
    delete_post,
    toggle_post_public,
)
from .privacy import (
    toggle_mute_user,
    toggle_block_user,
    list_blocked_users,
    list_muted_users,
    search_blocked_users,
    search_muted_users,
)
from .profile import (
    list_users,
    profile,
    my_avatar,
    get_user_profile,
    update_profile_settings,
    update_profile_avatar,
)
from .reels import reels_feed
from .reports import (
    report_post,
    report_user,
    report_page,
)
from .search import (
    search,
    search_history,
    search_posts,
    search_pages,
)

# Service helpers re-exported for backwards compatibility.
from ..services.auth_helpers import (
    _normalize_phone,
    _looks_like_email,
    _looks_like_phone,
    _find_user_by_identifier,
    _issue_tokens,
    _verify_google_id_token,
    _verify_facebook_access_token,
    _username_from_seed,
    _login_or_create_social_user,
)
from ..services.feed_helpers import (
    can_user_post_on_page,
    get_muted_page_ids,
    recency_decay,
    get_very_close_friend_ids,
    get_friend_ids,
    get_social_sets,
    get_social_overlap_score,
    _build_feed_context_uncached,
    serialize_post,
    merge_feed,
    build_feed_context,
    get_followed_feed,
    get_suggested_feed,
)
from ..services.media_processing import (
    _sniff_video_signature,
    verify_uploaded_media,
    _first_existing,
    resolve_overlay_font_path,
    _safe_float,
    _safe_int,
    _safe_optional_float,
    process_media_image,
    process_media_video,
)
