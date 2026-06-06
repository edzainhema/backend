"""
Email verification (M5).

Two-step JSON flow modeled on `password_reset.py`:

    /auth/email-verification-request/   POST { }   (Auth required)
        -> sends a fresh verification email to the caller's current
           User.email. No-op if the user has no email on file, or
           if the email is already verified.

    /auth/email-verification-confirm/   POST { uid, token } | { code }
        -> validates the token via Django's PasswordResetTokenGenerator
           (the same generator the password-reset flow uses, scoped to
           THIS user's password hash + last_login + verified state via
           a custom subclass) and flips UserProfile.email_verified = True.

Local registration (`register_user`) calls `send_verification_email`
inline (best-effort) so a fresh sign-up immediately receives the
verification mail; tokens are returned in the same response so the
user is logged in and can use the app while unverified. Sensitive
flows can later be gated on `UserProfile.email_verified` per-endpoint;
the audit explicitly leaves that as a separate decision.

Social auth (`_login_or_create_social_user`) already sets
`email_verified=True` when the provider verified the address, so
social users skip this flow entirely.

Token choice
------------
We subclass `PasswordResetTokenGenerator` so the verification token
incorporates the user's current `email_verified` flag into the hash
salt. Result: once the user successfully verifies, the same token
no longer validates (it auto-invalidates after first use). Same
shape Django's built-in tokens use; we don't roll our own.

Email change
------------
`update_profile_settings` clears `email_verified` when User.email
changes and triggers a fresh verification mail to the new address.
Without that, a user could claim a new email and any future
`email_verified` gates would still consider them verified for the
old address.
"""
import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

logger = logging.getLogger(__name__)


class _EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    """
    Like the default password-reset generator, but mixes the user's
    `email_verified` flag into the hash salt so a successful verify
    auto-invalidates the same token (the flag flips True, the hash
    changes, the next check fails).

    Also mixes in the user's current email so a token issued for
    email A doesn't accidentally verify email B after the user
    changes their email between request and confirm.
    """

    def _make_hash_value(self, user, timestamp):
        # PasswordResetTokenGenerator's default mixes in user.pk,
        # user.password, user.last_login, timestamp, and (in newer
        # Django) user.email. We add `email_verified` so the flag
        # flip invalidates the token, plus an explicit email mixin
        # for the rare older Django where the parent doesn't.
        base = super()._make_hash_value(user, timestamp)
        profile = getattr(user, "userprofile", None)
        verified = bool(profile and profile.email_verified)
        email = user.email or ""
        return f"{base}|verified={verified}|email={email}"


_token_generator = _EmailVerificationTokenGenerator()


def _build_verify_link(uid: str, token: str) -> str:
    """Universal link the email points at. Mirrors password reset:
    deep-links to the app's VerifyEmail screen when route resolution
    succeeds (per `frontend/docs/SETUP_PASSWORD_RESET_DEEP_LINK.md`'s
    pattern), otherwise opens the URL in a browser."""
    base = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    return f"{base}/verify-email?uid={uid}&token={token}"


def send_verification_email(user) -> None:
    """Send a verification email to `user`'s current address. Silently
    no-ops when:
      - user has no email on file (registered with phone only)
      - user.is_active is False (suspended accounts shouldn't get mail)
      - user is already verified

    Best-effort delivery: a failure logs and returns. Callers (notably
    `register_user`) treat this as fire-and-forget so a flaky SES
    can't break registration.
    """
    if not user.email or not user.is_active:
        return

    profile = getattr(user, "userprofile", None)
    if profile and profile.email_verified:
        # Already verified -- don't waste an email (or worse, confuse
        # the user with a "verify your email" mail they don't need).
        return

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = _token_generator.make_token(user)
    link = _build_verify_link(uid, token)

    subject = "Verify your Here Social email"
    body = (
        f"Hi {user.username},\n\n"
        f"Welcome to Here Social! To verify your email address, open this "
        f"link on your phone:\n"
        f"{link}\n\n"
        f"Or, if the link doesn't open the app, paste this code into the "
        f"Verify Email screen:\n\n"
        f"  Code: {uid}.{token}\n\n"
        f"This link will expire in 3 days. If you didn't sign up for "
        f"Here Social, you can safely ignore this email.\n\n"
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
        logger.warning(
            "email verification send failed for user %s: %s", user.id, exc,
        )


def confirm_email_verification(uid_b64: str, token: str):
    """Validate (uid, token) and flip `email_verified=True` on success.

    Returns (user, None) on success or (None, error_message) on failure.
    Same error-shape as `password_reset.confirm_password_reset`.

    Error messages are intentionally generic for the bad-uid /
    bad-token cases -- either would otherwise narrow an enumeration
    attempt. The success path also clears any auto-created
    UserProfile row's `email_verified` flip via .save() with explicit
    update_fields so other profile fields aren't accidentally touched.
    """
    if not uid_b64 or not token:
        return None, "Missing required fields."

    try:
        uid = force_str(urlsafe_base64_decode(uid_b64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return None, "Invalid or expired verification link."

    if not _token_generator.check_token(user, token):
        return None, "Invalid or expired verification link."

    if not user.email:
        # Edge case: user removed their email between request and
        # confirm. Refuse rather than silently mark "verified for
        # nothing."
        return None, "No email on file for this account."

    # Older accounts (or one-off race during signup) may not have
    # a UserProfile row yet -- create one so the verified flag has
    # somewhere to live.
    from ..models import UserProfile
    profile, _ = UserProfile.objects.get_or_create(user=user)
    if profile.email_verified:
        # Already verified is success too -- idempotent.
        return user, None

    profile.email_verified = True
    profile.save(update_fields=["email_verified"])
    return user, None
