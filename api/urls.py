from django.urls import path
from . import views
from rest_framework_simplejwt.views import TokenRefreshView, TokenVerifyView

urlpatterns = [

	# ---------------- Health ----------------
	# Lightweight liveness/readiness probe for uptime monitors and
	# (eventually) load balancer health checks. Public, ~5 ms, no DB
	# writes. See api/views/health.py.
	path("health/", views.health_check),

	# ---------------- Auth ----------------
	path("auth/register/", views.register_user),
	path("auth/login/", views.login_user),
	path("auth/social/", views.social_auth),
	path("auth/token/refresh/", TokenRefreshView.as_view()),
	path("auth/verify/", TokenVerifyView.as_view()),

	# ---------------- Profiles ----------------
	path("auth/profile/", views.profile),               # my profile
	path("auth/me/avatar/", views.my_avatar),           # lightweight: avatar URL only
	path("auth/user-profile/", views.get_user_profile), # other user's profile
	path("auth/users/", views.list_users),              # list users
	path("auth/memories/", views.get_user_memories),
	path("auth/memories/toggle/", views.toggle_page_memory),
	path(
		"auth/profile/settings/",
		views.update_profile_settings
	),
	path("auth/profile/avatar/", views.update_profile_avatar),


	# ---------------- Media ----------------

	path("posts/create/", views.create_post),

	# ---------------- Follow system ----------------
	path("auth/follow/", views.toggle_follow),
	path("auth/follow/approve/", views.approve_follow_request),
	path("auth/follow/reject/", views.reject_follow_request),
	path("auth/followers/", views.list_my_followers),
	path("auth/followers/remove/", views.remove_my_follower),
	path("auth/register-device/", views.register_device),
	path("auth/unregister-device/", views.unregister_device),
	path("auth/update-location/", views.update_user_location),

	# ---------------- Privacy ----------------
	path("auth/mute/", views.toggle_mute_user),
	path("auth/block/", views.toggle_block_user),
	path("auth/muted-users/", views.list_muted_users),
	path("auth/blocked-users/", views.list_blocked_users),
	path("auth/blocked-users/search/", views.search_blocked_users),
	path("auth/muted-users/search/", views.search_muted_users),


	# ---------------- Pages ----------------
	path("pages/create/", views.create_page),
	path("pages/detail/", views.get_page_detail),
	path("pages/follow/", views.toggle_page_follow),
	path("pages/follow/approve/", views.approve_page_follow_request),
	path("pages/follow/reject/", views.reject_page_follow_request),
	path("pages/", views.list_pages),
	path("pages/mute/", views.toggle_page_mute),
	path("pages/pin/", views.toggle_page_pin),
	path("pages/posters/", views.get_page_posters),
	path("pages/posters/toggle/", views.toggle_page_poster),
	path("pages/settings/", views.update_page_settings),
	path("pages/location/autocomplete/", views.places_autocomplete),
	path("pages/location/details/", views.place_details),
	path("pages/location/set/", views.set_page_location),
	path("pages/posters/search/", views.search_page_posters),
	path("pages/followers/", views.get_page_followers),
	path("pages/followers/remove/", views.remove_page_follower),
	path("pages/avatar/", views.update_page_avatar),
	path("pages/invite/", views.invite_to_page),
	path("pages/invite/cancel/", views.cancel_page_invite),
	path("pages/invite/respond/", views.respond_to_page_invite),
	path("pages/invite/search/", views.search_users_for_page_invite),
	path("pages/invite/sent/", views.list_sent_page_invites),

	# ---------------- Page Chat ----------------
	path("pages/chat/toggle/", views.toggle_page_chat),
	path("pages/chat/messages/", views.list_page_chat_messages),
	path("pages/chat/send/", views.send_page_chat_message),

	# ---------------- Notifications ----------------
	path("notifications/", views.list_notifications),
	path("notifications/unread-count/", views.unread_notifications_count),
	path("notifications/read/", views.mark_notification_read),
	path("notifications/read-all/", views.mark_all_notifications_read),

	# ---------------- Messaging ----------------
	path("auth/conversations/", views.list_conversations),
	path("auth/start-conversation/", views.start_conversation),
	path("auth/messages/", views.get_messages),
	path("auth/send-message/", views.send_message),
	path("auth/start-group-conversation/", views.start_group_conversation),
	path("auth/messages/search-users/", views.search_message_users),
	path("auth/conversations/delete/", views.delete_conversation),
	path("auth/messages/react/", views.react_to_message),
	path("auth/messages/edit/", views.edit_message),
	path("auth/messages/delete/", views.delete_message),
	path("auth/conversations/rename/", views.rename_conversation),

	# ---------------- Feed ----------------
	path("feed/", views.home_feed),

	# ---------------- Posts ----------------
	path("posts/like/", views.toggle_post_like),
	path("posts/save/", views.toggle_post_save),
	path("posts/not-interested/", views.not_interested),
	path("posts/saved/", views.saved_posts),
	path("posts/delete/", views.delete_post),
	path("posts/public-override/", views.toggle_post_public),

	# ---------------- Reels ----------------
	path("reels/", views.reels_feed),


	# ---------------- Search ----------------
	path("search/", views.search),
	path("search/posts/", views.search_posts),
	path("pages/search/", views.search_pages),
	path("search/history/", views.search_history),

	# ---------------- Comments ----------------
	path("comments/", views.get_comments),
	path("comments/create/", views.create_comment),
	path("comments/delete/", views.delete_comment),
	path("comments/like/", views.toggle_comment_like),

	# ---------------- Reports ----------------
	path("posts/report/", views.report_post),
	path("users/report/", views.report_user),
	path("pages/report/", views.report_page),

	# ---------------- Explore ----------------
	path("explore/", views.explore_feed),

	# ---------------- Analytics ----------------
	path(
		"analytics/profile-visit/",
		views.log_profile_visit
	),
	path(
		"analytics/video-watch/",
		views.log_video_watch
	),

	# ---------------- Activity tracking ----------------
	path("activity/post-view/",      views.log_post_view),
	path("activity/post-dwell/",     views.log_post_dwell),
	path("activity/comment-scroll/", views.log_comment_scroll),
	path("activity/post-share/",     views.log_post_share),
	path("activity/reel-watch/",     views.log_reel_watch),
	path("activity/search-click/",   views.log_search_click),
	path("activity/hashtag/",        views.log_hashtag_engagement),
	path("activity/tab-view/",       views.log_tab_view),
	# Batched ingest — the client buffers high-volume analytics and flushes
	# them here in bulk instead of one POST per event.
	path("activity/batch/",          views.log_activity_batch),
]
