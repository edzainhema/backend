

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken



from ..models import (
    UserProfile,
)
from ..services.auth_helpers import (
    _find_user_by_identifier, _issue_tokens, _login_or_create_social_user,
    _looks_like_email, _looks_like_phone, _normalize_phone, _verify_facebook_access_token,
    _verify_google_id_token,
)
from ..services.email_verification import (
    send_verification_email, confirm_email_verification,
)
from ..services.password_reset import (
    request_password_reset, confirm_password_reset,
)
from ..services.throttles import (
    LoginRateThrottle, RegisterRateThrottle, SocialAuthRateThrottle,
    LogoutRateThrottle, PasswordResetRequestThrottle,
    PasswordResetConfirmThrottle,
    EmailVerificationRequestThrottle, EmailVerificationConfirmThrottle,
)


@api_view(['POST'])
@throttle_classes([RegisterRateThrottle])
def register_user(request):
	"""
	Create an account with either a phone number or an email address.

	Expected body:
		{
		  "username": "<unique handle>",
		  "password": "<password>",
		  "identifier_type": "phone" | "email",
		  "identifier": "<phone number or email address>"
		}

	Legacy clients that send only {username, password} still work.
	"""
	username = (request.data.get('username') or '').strip()
	password = request.data.get('password') or ''
	identifier_type = (request.data.get('identifier_type') or '').lower()
	identifier = (request.data.get('identifier') or '').strip()

	if not username or not password:
		return Response({"error": "Username and password are required"}, status=400)

	if User.objects.filter(username__iexact=username).exists():
		return Response({"error": "Username already taken"}, status=400)

	# Enforce AUTH_PASSWORD_VALIDATORS at registration (audit H2). The user
	# row doesn't exist yet, so user=None -- the
	# UserAttributeSimilarityValidator silently no-ops without a user, but
	# the minimum-length / common-password / numeric-password checks all
	# still run. Same error-shape pattern as services/password_reset.py:
	# join the first round of validator complaints into one string.
	try:
		validate_password(password, user=None)
	except ValidationError as exc:
		return Response({"error": "; ".join(exc.messages)}, status=400)

	email = ''
	phone = ''

	if identifier:
		if identifier_type == 'email' or (not identifier_type and _looks_like_email(identifier)):
			if not _looks_like_email(identifier):
				return Response({"error": "Invalid email address"}, status=400)
			email = identifier.lower()
			if User.objects.filter(email__iexact=email).exists():
				return Response({"error": "Email already in use"}, status=400)
		elif identifier_type == 'phone' or (not identifier_type and _looks_like_phone(identifier)):
			if not _looks_like_phone(identifier):
				return Response({"error": "Invalid phone number"}, status=400)
			phone = _normalize_phone(identifier)
			if UserProfile.objects.filter(phone_number=phone).exists():
				return Response({"error": "Phone number already in use"}, status=400)
		else:
			return Response({"error": "Unrecognized identifier"}, status=400)

	# H3: the .exists() pre-checks above are TOCTOU-racy with the create
	# calls below -- two simultaneous registers with the same
	# username/email/phone can both pass the check and both attempt
	# .create(). The DB-level UNIQUE constraints (migration 0099) make
	# the second insert raise IntegrityError. Wrap in atomic() so the
	# User + UserProfile rows commit together (no orphan User with a
	# missing profile if the phone-uniqueness race hits the second
	# insert), and turn IntegrityError into a 400 with the correct
	# field-specific message by re-checking which identifier collided.
	try:
		with transaction.atomic():
			user = User.objects.create(
				username=username,
				email=email,
				password=make_password(password),
			)
			UserProfile.objects.create(user=user, phone_number=phone)
	except IntegrityError:
		# Re-check to identify which field collided so we can give the
		# user the right message. These queries run in a fresh
		# transaction (atomic() rolled back before re-raising), so they
		# see the row that beat us to the insert.
		if User.objects.filter(username__iexact=username).exists():
			return Response({"error": "Username already taken"}, status=400)
		if email and User.objects.filter(email__iexact=email).exists():
			return Response({"error": "Email already in use"}, status=400)
		if phone and UserProfile.objects.filter(phone_number=phone).exists():
			return Response({"error": "Phone number already in use"}, status=400)
		# Constraint fired but we can't see which row caused it (rare:
		# the winning row could have been deleted between our race
		# and this re-check). Give the client a non-500 generic.
		return Response(
			{"error": "Could not create account, please try again."},
			status=409,
		)

	# M5: send a verification email if the user registered with one.
	# Best-effort, fire-and-forget -- a flaky SES must NOT fail an
	# otherwise successful registration. Users registered via phone
	# (no email on the user row) silently skip this branch; social
	# signups bypass it entirely via /auth/social/, which sets
	# email_verified=True up front from the provider claim.
	try:
		send_verification_email(user)
	except Exception:
		pass

	tokens = _issue_tokens(user)
	return Response({
		"message": "User registered successfully",
		**tokens,
	})


@api_view(['POST'])
@throttle_classes([LoginRateThrottle])
def login_user(request):
	"""
	Log in with username, email, or phone number + password.

	Accepts {identifier, password} (preferred) or legacy {username, password}.
	"""
	identifier = (
		request.data.get('identifier')
		or request.data.get('username')
		or ''
	).strip()
	password = request.data.get('password') or ''

	if not identifier or not password:
		return Response({"error": "Identifier and password are required"}, status=400)

	user = _find_user_by_identifier(identifier)
	if not user or not user.check_password(password):
		return Response({"error": "Invalid credentials"}, status=400)

	return Response(_issue_tokens(user))


@api_view(['POST'])
@throttle_classes([SocialAuthRateThrottle])
def social_auth(request):
	"""
	Exchange a verified provider token for our own JWT pair.

	Body:
		{ "provider": "google", "id_token": "<google id token>" }
		{ "provider": "facebook", "access_token": "<fb access token>" }
	"""
	provider = (request.data.get('provider') or '').lower()

	if provider == 'google':
		payload = _verify_google_id_token(request.data.get('id_token'))
		if not payload:
			return Response({"error": "Could not verify Google token"}, status=400)
		# _verify_google_id_token only returns a payload for a Google-verified
		# email, so reaching here means the address is provider-verified.
		user = _login_or_create_social_user(
			email=payload.get('email'),
			full_name=payload.get('name') or '',
			provider='google',
			provider_id=payload.get('sub') or '',
			email_verified=True,
		)
		if user is None:
			return Response(
				{"error": "This email is registered to another sign-in method."},
				status=409,
			)
		return Response(_issue_tokens(user))

	if provider == 'facebook':
		payload = _verify_facebook_access_token(request.data.get('access_token'))
		if not payload:
			return Response({"error": "Could not verify Facebook token"}, status=400)
		# Facebook's Graph API only returns an email when it is a
		# Facebook-confirmed address on the account, so treat a returned email
		# as provider-verified.
		user = _login_or_create_social_user(
			email=payload.get('email'),
			full_name=payload.get('name') or '',
			provider='facebook',
			provider_id=payload.get('id') or '',
			email_verified=bool(payload.get('email')),
		)
		if user is None:
			return Response(
				{"error": "This email is registered to another sign-in method."},
				status=409,
			)
		return Response(_issue_tokens(user))

	return Response({"error": "Unsupported provider"}, status=400)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([LogoutRateThrottle])
def logout_user(request):
	"""
	Revoke a refresh token so it can no longer mint access tokens.

	Body: {"refresh": "<refresh token>"}

	The token's jti is written to the simplejwt blacklist
	(rest_framework_simplejwt.token_blacklist) so any subsequent
	/auth/token/refresh/ call with the same refresh token returns 401.

	The endpoint is intentionally idempotent and tolerant:
	  - missing / malformed / already-blacklisted / expired tokens all
	    return 200 with status='ok'. A logout that "succeeds" client-side
	    must not look like it failed when the server-side state was already
	    the desired one -- otherwise sign-out gets stuck in a retry loop
	    on a flaky network or after an app reinstall.
	  - permission_classes is AllowAny: a user whose access token has
	    already expired must still be able to log out the refresh token.
	    The refresh token itself is the credential being revoked, so its
	    own signature is the authorization for this call.

	The active session's access token is NOT blacklisted here -- it expires
	on its own within ACCESS_TOKEN_LIFETIME (1 hour) and there is no
	server-side store of access tokens to invalidate. Clients should clear
	the access token locally; an attacker holding only an access token
	loses access at the next hour boundary regardless.
	"""
	raw = (request.data.get('refresh') or '').strip()
	if not raw:
		# Treat as a no-op success: the caller wants to be logged out, and
		# without a refresh token there's nothing to revoke. Returning 400
		# here would force the frontend to handle "already logged out" as
		# an error in every sign-out path.
		return Response({"status": "ok"})

	try:
		token = RefreshToken(raw)
		token.blacklist()
	except TokenError:
		# Malformed, expired, or already-blacklisted token. The desired
		# end state -- "this refresh token cannot be used" -- is already
		# true, so report success.
		pass

	return Response({"status": "ok"})


# Generic response used by every branch of password-reset-request so the
# endpoint can't be turned into a user-enumeration oracle. Worded so the
# UI can render it verbatim ("we sent a link IF the address exists") --
# no follow-up state, no error vs success distinction visible to the
# caller.
_RESET_REQUEST_OK_BODY = {
	"status": "ok",
	"message": (
		"If an account exists for that email, a password-reset link is on "
		"its way. Check your inbox in a few minutes."
	),
}


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetRequestThrottle])
def password_reset_request(request):
	"""
	Start the password-reset flow.

	Body: {"identifier": "<email | username | phone>"}

	Always returns 200 with the same generic body whether the identifier
	matched a user or not -- the request endpoint must not double as a
	"does this email exist?" oracle. The matching branch sends an email
	containing a single-use link + code; the non-matching branch silently
	no-ops. See services/password_reset.py.

	AllowAny: a logged-out user is the entire point of this endpoint.

	Rate-limiting: NOT enforced here -- audit B4 calls for the request
	endpoint to be throttled (per-IP, per-identifier) before launch so it
	can't be used to spam the SES quota. Add the throttle in front of
	this view when B4 lands; the service-layer call is already cheap on
	the no-match branch.
	"""
	identifier = (request.data.get("identifier") or "").strip()
	# Always do the lookup-and-maybe-send, even when the identifier is
	# blank, so the response time doesn't visibly branch on "non-empty
	# input means a real lookup happened." request_password_reset itself
	# no-ops cleanly on empty / unknown input.
	try:
		request_password_reset(identifier)
	except Exception:
		# Swallow everything: a transient failure must not leak through
		# as a 500 (or worse, a stack trace) and tell an attacker their
		# input made the server unhappy in a particular way.
		pass
	return Response(_RESET_REQUEST_OK_BODY)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetConfirmThrottle])
def password_reset_confirm(request):
	"""
	Complete the password-reset flow.

	Body: {"uid": "<base64 uid>", "token": "<reset token>", "new_password": "..."}

	On success:
	  - the password is updated (with AUTH_PASSWORD_VALIDATORS enforced --
	    the same validators that registration silently bypasses today;
	    see audit H2)
	  - every outstanding refresh token for the user is blacklisted, so a
	    reset triggered because a token leaked actually closes that hole
	    (audit B2 + this fix)
	  - the response is 200 with status='ok'; the client should route the
	    user to Login with a "password updated" toast

	On failure (invalid token, weak password, expired link, etc.) returns
	400 with a user-safe error string. The token check itself returns the
	same "Invalid or expired reset link" message for both bad-uid and
	bad-token cases so neither can be used to enumerate users.

	AllowAny by the same reasoning as password_reset_request: the user
	doing the reset has no working session, by definition.
	"""
	uid = (request.data.get("uid") or "").strip()
	token = (request.data.get("token") or "").strip()
	new_password = request.data.get("new_password") or ""

	# Support a one-field "code" form (the value we print in the email
	# body, "<uid>.<token>") so users on devices without deep-link
	# routing can copy-paste it into the in-app screen as a single string.
	if not uid and not token:
		code = (request.data.get("code") or "").strip()
		if "." in code:
			uid, _, token = code.partition(".")

	user, error = confirm_password_reset(uid, token, new_password)
	if error is not None:
		return Response({"error": error}, status=400)

	return Response({
		"status": "ok",
		"message": "Password updated. Please log in with your new password.",
	})


# Generic response used by every branch of email-verification-request so
# the endpoint can't double as a "does this user have an email on file"
# oracle for an authenticated attacker probing other accounts. Auth IS
# required (it's a resend for the caller's OWN address), but we still
# return the same body whether or not we actually sent a mail (the
# service silently no-ops on already-verified / no-email-on-file users).
_EMAIL_VERIFY_REQUEST_OK_BODY = {
	"status": "ok",
	"message": (
		"If your email needs verification, a fresh verification link is "
		"on its way. Check your inbox in a few minutes."
	),
}


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([EmailVerificationRequestThrottle])
def email_verification_request(request):
	"""
	Resend the email-verification mail to the caller's current email.

	Body: {} (no fields -- the user is identified by the bearer token)

	IsAuthenticated because a logged-out user can't ask us to mail
	their own address (a server we trust doesn't know it's "their"
	address without proof). A logged-out user with the original
	verification link can still hit /auth/email-verification-confirm/
	directly (AllowAny on that endpoint).

	Silently no-ops on:
	  - users with no email on file (phone-only signups)
	  - already-verified users
	  - inactive accounts
	`send_verification_email` makes those calls inside the service so
	this view doesn't have to know about them. Always returns 200 with
	the same generic body so the response shape doesn't leak which
	branch fired.

	Rate-limited at 3/min per user via EmailVerificationRequestThrottle
	to bound SES spend if a client loops the resend button.
	"""
	try:
		send_verification_email(request.user)
	except Exception:
		# Swallow everything: a transient failure must not crash the
		# response or look different from the "no email needed" branch.
		pass
	return Response(_EMAIL_VERIFY_REQUEST_OK_BODY)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([EmailVerificationConfirmThrottle])
def email_verification_confirm(request):
	"""
	Confirm an email-verification token and flip
	`UserProfile.email_verified = True`.

	Body: {"uid": "<base64 uid>", "token": "<verification token>"}
	   OR {"code": "<uid>.<token>"}  (paste-from-email fallback)

	AllowAny: the token IS the credential (a user with the link in
	their email can verify even if they're not currently logged in).
	Same shape and trust model as /auth/password-reset-confirm/.

	On success returns 200; the client should toast "Email verified"
	and refresh the profile blob so the new email_verified=True is
	visible in the UI. No session change happens here -- the user's
	tokens (if any) are unaffected.

	On failure returns 400 with a user-safe error string. The
	bad-uid and bad-token cases share the SAME error message so the
	endpoint can't be used to enumerate accounts.
	"""
	uid = (request.data.get("uid") or "").strip()
	token = (request.data.get("token") or "").strip()

	# Support a one-field "code" form (the value we print in the email
	# body, "<uid>.<token>") so users on devices without deep-link
	# routing can copy-paste it into the in-app screen as one string.
	# Mirrors the password-reset-confirm endpoint's convenience input.
	if not uid and not token:
		code = (request.data.get("code") or "").strip()
		if "." in code:
			uid, _, token = code.partition(".")

	user, error = confirm_email_verification(uid, token)
	if error is not None:
		return Response({"error": error}, status=400)

	return Response({
		"status": "ok",
		"message": "Email verified.",
	})
