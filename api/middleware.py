from .services.session_context import (
    clear_current_session_id, sanitize_session_id, set_current_session_id,
)


class SessionIdMiddleware:
    """
    Capture the client's X-Session-Id header into request-local state so
    log_activity can tag every Activity row with the session it belongs to
    (C3). Add to settings.MIDDLEWARE:

        'api.middleware.SessionIdMiddleware',

    Also exposes the sanitized value as request.session_id for views (e.g. the
    feed composer tags its impression rows from it).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        sid = sanitize_session_id(request.headers.get("X-Session-Id"))
        request.session_id = sid
        set_current_session_id(sid)
        try:
            return self.get_response(request)
        finally:
            # Always clear so the value can't bleed into the next request that
            # reuses this worker thread / task.
            clear_current_session_id()


class UpdateLastSeenMiddleware:
    """
    Updates UserProfile.last_seen on every authenticated request.
    Add to settings.MIDDLEWARE after AuthenticationMiddleware:

        'api.middleware.UpdateLastSeenMiddleware',

    This drives the is_online property on UserProfile and the
    is_online field on BasicUserSerializer.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Update last_seen after the response so it doesn't add latency
        if hasattr(request, 'user') and request.user.is_authenticated:
            # Throttled (WS-2): this hot-row write now happens at most once per
            # ~45s per user instead of on every request. touch_last_seen is
            # best-effort and never raises, so it can't break the response.
            from .services.presence import touch_last_seen
            touch_last_seen(request.user.id)
        return response
