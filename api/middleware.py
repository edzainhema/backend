from .services.session_context import (
    clear_current_session_id, sanitize_session_id, set_current_session_id,
)


class RealClientIPMiddleware:
    """B4: promote `X-Forwarded-For`'s LAST entry to `REMOTE_ADDR` so
    DRF's `AnonRateThrottle` keys on the real client IP, not on the
    nginx host that proxied to us, and not on an attacker-controlled
    value either.

    Without this, in production behind nginx, every anonymous request
    appears to come from the nginx host's loopback / VPC IP. The
    per-IP anon throttle buckets then collapse into one global bucket
    per scope -- a single attacker exhausts the bucket for every other
    anonymous user (self-DoS). Same effect for any other code that
    reads `REMOTE_ADDR` (analytics, IP-based audit logging, etc.).

    Trust model -- H-1 fix
    ----------------------
    nginx in front of us uses the standard
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    which APPENDS the real TCP peer to whatever the client already
    sent. So a request arriving with a forged client-side
        X-Forwarded-For: 1.2.3.4
    reaches Django as
        X-Forwarded-For: 1.2.3.4, <real-client-ip>
    The LAST entry is the one nginx itself wrote -- it is by
    definition the actual TCP peer connecting to nginx, and
    nothing the client sends can move it. We therefore read
    `xff.split(",")[-1]`, never `[0]`.

    This assumes EXACTLY ONE trusted proxy (nginx). If you ever add a
    second hop in front of nginx (a load balancer, a CDN with
    origin-shield, etc.), the LAST entry becomes that hop's view of
    the world and you must instead count N hops from the end, where
    N = number of trusted proxies. If Django is ever exposed
    directly to the internet (no nginx), drop this middleware
    entirely -- `REMOTE_ADDR` then is the real peer.

    Ordering: place this BEFORE every middleware / view that reads
    `REMOTE_ADDR`. In our `MIDDLEWARE` list, it sits right after
    `SecurityMiddleware` (which is always first) and before everything
    else.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if xff:
            # `X-Forwarded-For: <attacker-controlled...>, <real-client-ip>`
            # nginx ($proxy_add_x_forwarded_for) appends the peer IP to
            # whatever the client already sent. The LAST entry is the
            # only one nginx itself wrote -- everything before it could
            # have been spoofed. Take the last, trimmed of whitespace.
            # (See PRODUCTION_READINESS_AUDIT.md H-1.)
            request.META["REMOTE_ADDR"] = xff.split(",")[-1].strip()
        return self.get_response(request)


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
