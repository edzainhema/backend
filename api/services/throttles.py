"""
Per-endpoint rate-limit throttles (B4).

DRF's `AnonRateThrottle` and `UserRateThrottle` are the right primitives,
but they bind exactly one rate (`anon` / `user`) globally. We want
DIFFERENT caps on different endpoints -- a stricter cap on `/auth/login/`
than on `/feed/`, etc. -- so we subclass each base class once per scope
and bind the scope to a separate `DEFAULT_THROTTLE_RATES` entry in
settings.py. The DRF-standard alternative is `ScopedRateThrottle` with
a per-view `throttle_scope` attribute, but that attribute pattern is
awkward to apply cleanly to `@api_view`-wrapped function-based views
(the wrapper hides the attribute from the lookup); a subclass-per-scope
gets the same effect with less ceremony.

Identification:
  - AnonRateThrottle subclasses key on REMOTE_ADDR and skip authenticated
    requests entirely (DRF returns None from get_cache_key when the user
    is authenticated). Use these for endpoints anyone can reach
    unauthenticated -- login / register / social / logout / password
    reset. A signed-in user hitting these is unaffected (which is fine;
    they'll never need to in practice).
  - UserRateThrottle subclasses key on user.pk when authenticated,
    REMOTE_ADDR when not. Use these for endpoints behind IsAuthenticated
    where we want a per-account cap. Unauthenticated callers get bounced
    by IsAuthenticated before the throttle ever runs, so the IP fallback
    is irrelevant on those.

Behind nginx:
  Throttles rely on `request.META['REMOTE_ADDR']` for the anonymous /
  unauthenticated cache key. In production behind nginx, that's the
  proxy's address unless `X-Forwarded-For` is forwarded AND Django is
  configured to trust it. Today nginx already sends
  `proxy_set_header X-Forwarded-Proto $scheme;` (settings.py
  SECURE_PROXY_SSL_HEADER) -- the matching `X-Forwarded-For` is the
  defense-in-depth follow-up the audit calls for. Without it, every
  anonymous request looks like it's coming from the nginx host's IP and
  the per-IP buckets collapse into one global bucket per scope. Easy
  fix at deploy time, irrelevant in local dev (single process, real
  REMOTE_ADDR).

Storage:
  DRF stores counters in `caches['default']`. Production runs Redis
  (settings.py INF-2), so counters are shared across worker processes.
  Local dev's LocMemCache makes counters per-process, which is fine
  there because dev is single-worker.
"""
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


# ---------------------------------------------------------------------------
# Anonymous / auth-entry endpoints. Caps the IP, not the account -- a
# logged-out attacker has no account to cap on.
# ---------------------------------------------------------------------------

class LoginRateThrottle(AnonRateThrottle):
    """Credential stuffing: cap login attempts per IP."""
    scope = 'auth_login'


class RegisterRateThrottle(AnonRateThrottle):
    """Fake-account creation: tighter cap than login since each call writes
    a row + sends auth tokens back."""
    scope = 'auth_register'


class SocialAuthRateThrottle(AnonRateThrottle):
    """Caps social-login attempts (Google / Facebook). Same shape as
    LoginRateThrottle -- different scope so we can tune independently
    if either provider gets abused first."""
    scope = 'auth_social'


class LogoutRateThrottle(AnonRateThrottle):
    """Logout is idempotent and the user-experience cost of a missed
    logout is real (an attacker is the worst case here, but locking the
    legitimate user out of signing out is the wrong defense). Looser cap."""
    scope = 'auth_logout'


class PasswordResetRequestThrottle(AnonRateThrottle):
    """Strictest scope in this file. Each call CAN send an SES email,
    so this is also a spend-protection throttle, not just a security
    cap. Layered with the per-account silent no-op in the service:
    even if a per-IP attacker fires 3 requests/min, none of them
    actually email the victim unless the identifier matches."""
    scope = 'password_reset_request'


class PasswordResetConfirmThrottle(AnonRateThrottle):
    """Brute-forcing the (uid, token) pair is the threat. Tokens are
    HMAC'd with the user's password hash + last_login, so the search
    space is large, but a slow cap closes the door."""
    scope = 'password_reset_confirm'


class EmailVerificationRequestThrottle(UserRateThrottle):
    """Auth required, so per-user not per-IP. Each call sends a fresh
    SES email; strict cap to bound spend and abuse-by-self (e.g. a
    misbehaving client looping the resend button). Lower than the
    password-reset request cap because a freshly-registered user
    already got one verification mail in their registration flow."""
    scope = 'email_verification_request'


class EmailVerificationConfirmThrottle(AnonRateThrottle):
    """Anonymous, by-IP -- the token IS the credential, so a logged-out
    user with the email link must be able to verify. Brute-forcing the
    (uid, token) pair is the threat; same reasoning as the
    password-reset confirm throttle."""
    scope = 'email_verification_confirm'


# ---------------------------------------------------------------------------
# Authenticated write endpoints. Caps the account, not the IP -- a single
# attacker on a botnet can rotate IPs but not accounts cheaply.
# ---------------------------------------------------------------------------

class FollowRateThrottle(UserRateThrottle):
    """Mass-follow / unfollow abuse: each follow generates a Notification
    + a push to the target, so this is also push-spam protection."""
    scope = 'follow'


class SendMessageRateThrottle(UserRateThrottle):
    """DM spam + per-recipient push fan-out. Group conversations
    multiply the push cost, so the cap is per-sender, not per-message."""
    scope = 'send_message'


class PostCreateRateThrottle(UserRateThrottle):
    """Post-flooding. Lower than other writes because each post can fan
    out @mention pushes, hashtag indexing, and feed-cache invalidations
    across every follower."""
    scope = 'post_create'


class CommentCreateRateThrottle(UserRateThrottle):
    """Comment spam + comment-mention push fan-out. Higher than
    post_create because legit comment activity (bursts on a viral
    thread) is naturally higher."""
    scope = 'comment_create'


class PostEngagementRateThrottle(UserRateThrottle):
    """Like / save toggles. Generous because real users double-tap
    quickly on a fast scroll. The cap is here to bound notification
    fan-out and `viewer_can_see_post` enumeration probes -- not to
    police normal tapping."""
    scope = 'post_engagement'


class ReportRateThrottle(UserRateThrottle):
    """Report endpoints (post / user / page). Reports go to a human
    moderation queue (eventually); a flood here is mostly a queue-DoS
    vector, not a user-facing risk. Strict cap reflects that."""
    scope = 'report'


class PageInviteRateThrottle(UserRateThrottle):
    """Invite spam + push fan-out to invitees. Same per-account
    rationale as FollowRateThrottle."""
    scope = 'page_invite'
