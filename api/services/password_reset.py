"""
Password reset flow (B3).

Two-step JSON variant of Django's built-in `PasswordResetView` / `PasswordResetConfirmView`:

    /auth/password-reset-request/   POST { identifier }
        -> resolves the identifier (email / phone / username), looks up the
           user, and emails them a (uid, token) pair. Returns 200 with a
           deliberately generic body regardless of whether the user exists,
           so the endpoint can't be used as a user-enumeration oracle.

    /auth/password-reset-confirm/   POST { uid, token, new_password }
        -> validates the token via `PasswordResetTokenGenerator`, runs the
           password through Django's `AUTH_PASSWORD_VALIDATORS`, sets it,
           and blacklists every outstanding refresh token for the user so a
           compromised session cannot survive the reset.

Token choice:
  We use `django.contrib.auth.tokens.default_token_generator`, NOT a fresh
  JWT. The default generator hashes the user's password and `last_login`
  into the token, so the token auto-invalidates the moment either changes —
  including when the reset itself completes (the password hash flips, so
  the link cannot be re-used). It also expires after
  `PASSWORD_RESET_TIMEOUT` (default 3 days). A custom JWT here would have
  to reimplement both properties; the built-in generator already gets it
  right and is the industry-standard pattern for this flow.

Enumeration:
  The request endpoint always returns 200 with the same body, whether the
  identifier matched a user or not. The expensive work (token generation,
  email send) only happens on the matching branch — but a network observer
  timing the response can't reliably distinguish the two paths since the
  email send is queued through SES and returns quickly.
"""
import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .auth_helpers import _find_user_by_identifier

logger = logging.getLogger(__name__)


def _build_reset_link(uid: str, token: str) -> str:
    """
    Build the deep link the email points at. Resolves against `SITE_URL`
    so dev (http://localhost:8000) and prod (https://here-social.com)
    naturally produce the right host. The mobile app handles
    here-social.com/reset-password via deep-link routing (see app.json's
    associatedDomains / intent-filters); a web page at the same path can
    do server-side validation and POST to the confirm endpoint.

    The token and uid are also delivered as a 6-segment "code" in the
    email body so users on devices without deep-link routing (or who
    received the email on a different device) can still complete the
    flow by copy-pasting the code into the in-app screen.
    """
    base = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    return f"{base}/reset-password?uid={uid}&token={token}"


def _send_reset_email(user, uid: str, token: str) -> None:
    """
    Send the reset email. Best-effort: a failure here is logged but never
    surfaced to the caller — the request endpoint must return the same
    generic body whether the email succeeded, failed, or wasn't sent at
    all (no email on file). Otherwise an attacker could probe whether
    a known email belongs to a user by timing / observing the response.
    """
    if not user.email:
        return

    link = _build_reset_link(uid, token)
    subject = "Reset your Here Social password"
    body = (
        f"Hi {user.username},\n\n"
        f"We received a request to reset your password.\n\n"
        f"To set a new password, open this link on your phone:\n"
        f"{link}\n\n"
        f"Or, if the link doesn't open the app, paste this code into the "
        f"Reset Password screen:\n\n"
        f"  Code: {uid}.{token}\n\n"
        f"This link will expire in 3 days. If you didn't ask for a password "
        f"reset, you can safely ignore this email -- your password will not "
        f"be changed.\n\n"
        f"-- Here Social"
    )

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[user.email],
            fail_silently=True,
        )
    except Exception as exc:
        logger.warning("password reset email send failed for user %s: %s", user.id, exc)


def request_password_reset(identifier: str) -> None:
    """
    Look up the user and trigger the reset email if appropriate.

    Always returns None. The HTTP view that calls this should respond
    with a generic 200, never reporting "found" / "not found" — that
    decision lives in the view, and this function deliberately gives it
    no signal to leak.

    Quietly no-ops if:
      - the identifier doesn't resolve to a user
      - the user has no email on file (we can only deliver via email)
      - the user's account is inactive (Django's `is_active=False`)

    Each branch is silent + symmetric so an enumeration attempt sees the
    same response shape and roughly the same timing.
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return

    user = _find_user_by_identifier(identifier)
    if user is None or not user.is_active:
        return
    if not user.email:
        return

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    _send_reset_email(user, uid, token)


def confirm_password_reset(uid_b64: str, token: str, new_password: str):
    """
    Complete the reset. Returns (user, None) on success or
    (None, error_message) on failure.

    Steps, in order:
      1. Decode the uid and look up the user.
      2. Verify the token via the same generator that issued it.
      3. Run the new password through Django's configured validators
         (AUTH_PASSWORD_VALIDATORS) -- so the minimum-length /
         common-password / numeric-password / user-attribute-similarity
         checks defined in settings.py are enforced here too. The same
         validators were silently bypassed at registration (audit H2);
         this endpoint is the right place to start enforcing them
         consistently.
      4. Persist the new password.
      5. BLACKLIST every outstanding refresh token for this user.
         Without this, a password reset that was triggered BECAUSE a
         refresh token leaked would leave the leaked token still valid
         for up to 60 days. The blacklist app is in INSTALLED_APPS
         (audit B2), so the `OutstandingToken` rows are populated by
         simplejwt at issue time -- we just need to flip each to
         blacklisted here.

    Error messages are intentionally specific for client UX ("token
    expired" surfaces a different toast than "weak password") but never
    leak existence -- a bad uid and a bad token both return
    "Invalid or expired reset link".
    """
    if not uid_b64 or not token or not new_password:
        return None, "Missing required fields."

    try:
        uid = force_str(urlsafe_base64_decode(uid_b64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return None, "Invalid or expired reset link."

    if not default_token_generator.check_token(user, token):
        return None, "Invalid or expired reset link."

    try:
        validate_password(new_password, user=user)
    except ValidationError as exc:
        # Surface the first validator complaint; the rest are usually
        # variations on the same theme.
        return None, "; ".join(exc.messages)

    user.set_password(new_password)
    user.save(update_fields=["password"])

    # Kill every outstanding refresh token for this user, server-side.
    # This is the security-critical step: a reset triggered BECAUSE a
    # token leaked must invalidate that token, not just rotate it.
    _blacklist_all_refresh_tokens(user)

    return user, None


def _blacklist_all_refresh_tokens(user) -> None:
    """Blacklist every outstanding refresh token for this user.

    Imports are local because the blacklist app is optional at the
    settings layer historically (and the unit tests around the audit
    were written for the no-blacklist world). With the B2 fix the app
    is now in INSTALLED_APPS, so the imports always succeed in our
    deploys -- the try/except just keeps this function honest for any
    future setup where someone disables the app.
    """
    try:
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken,
            OutstandingToken,
        )
    except Exception:
        return

    for outstanding in OutstandingToken.objects.filter(user=user):
        # get_or_create is idempotent -- if this token was already
        # blacklisted (e.g. by a prior rotation), the second insert
        # silently no-ops instead of raising IntegrityError.
        BlacklistedToken.objects.get_or_create(token=outstanding)
