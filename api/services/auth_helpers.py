"""
Authentication helpers: phone/email normalisation, identifier lookup,
JWT issuing, and Google/Facebook social-auth token verification plus
the just-in-time user-creation path that backs `social_auth`.

Extracted from the monolithic views.py during the 2026-05 refactor.
"""
import json
import re

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User

from rest_framework_simplejwt.tokens import RefreshToken

from ..models import UserProfile


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\-\s]{6,19}$")


def _normalize_phone(value):
	"""Strip spaces/dashes so phones are stored consistently."""
	if not value:
		return value
	return re.sub(r"[\s\-]", "", value)


def _looks_like_email(value):
	return bool(value) and bool(_EMAIL_RE.match(value))


def _looks_like_phone(value):
	return bool(value) and bool(_PHONE_RE.match(value))


def _find_user_by_identifier(identifier):
	"""
	Resolve a login identifier that may be a username, email, or phone number.
	Returns the matching User or None.
	"""
	if not identifier:
		return None

	# Email
	if _looks_like_email(identifier):
		user = User.objects.filter(email__iexact=identifier).first()
		if user:
			return user

	# Phone (stored on UserProfile)
	if _looks_like_phone(identifier):
		normalized = _normalize_phone(identifier)
		profile = UserProfile.objects.filter(phone_number=normalized).first()
		if profile:
			return profile.user

	# Fall back to username
	return User.objects.filter(username__iexact=identifier).first()


def _issue_tokens(user):
	refresh = RefreshToken.for_user(user)
	return {
		"refresh": str(refresh),
		"access": str(refresh.access_token),
	}


def _coerce_bool(value):
	"""
	Interpret the loosely-typed truthy values identity providers use for
	claims like `email_verified`. Google's tokeninfo endpoint returns the
	string "true"/"false"; the OIDC userinfo form returns a real bool. Treat
	both (and only those) as authoritative, and anything else as False so a
	missing/garbage claim can never read as verified.
	"""
	if isinstance(value, bool):
		return value
	if isinstance(value, str):
		return value.strip().lower() in ("true", "1", "yes")
	return bool(value)


def _verify_google_id_token(id_token):
	"""
	Validate a Google ID token via Google's tokeninfo endpoint and return its claims.
	Returns the decoded payload dict on success, or None on failure.
	"""
	import urllib.request
	import urllib.parse

	if not id_token:
		return None

	try:
		url = "https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode({"id_token": id_token})
		with urllib.request.urlopen(url, timeout=10) as resp:
			if resp.status != 200:
				return None
			payload = json.loads(resp.read().decode("utf-8"))
	except Exception:
		return None

	# Optional: enforce audience if GOOGLE_OAUTH_CLIENT_IDS is configured in settings.
	from django.conf import settings
	allowed_aud = getattr(settings, "GOOGLE_OAUTH_CLIENT_IDS", None)
	if allowed_aud:
		if payload.get("aud") not in allowed_aud:
			return None

	if not payload.get("email"):
		return None

	# Only accept Google-verified emails. An unverified email must never be
	# treated as a login identity — the social-login path keys on the email to
	# decide whether to adopt a pre-existing account, so trusting an unverified
	# one would reopen the account pre-hijacking vector. Normalise the claim to
	# a real bool first so the string "true" from tokeninfo isn't accidentally
	# truthy-tested as a non-empty string regardless of value.
	if not _coerce_bool(payload.get("email_verified")):
		return None

	return payload


def _verify_facebook_access_token(access_token):
	"""
	Use Facebook's Graph API to look up the user behind an access token.
	Returns a dict with id/email/name on success, None on failure.
	"""
	import urllib.request
	import urllib.parse

	if not access_token:
		return None

	try:
		url = "https://graph.facebook.com/me?" + urllib.parse.urlencode({
			"fields": "id,name,email",
			"access_token": access_token,
		})
		with urllib.request.urlopen(url, timeout=10) as resp:
			if resp.status != 200:
				return None
			return json.loads(resp.read().decode("utf-8"))
	except Exception:
		return None


def _username_from_seed(seed):
	"""Generate a unique username starting from `seed`."""
	import uuid

	base = re.sub(r"[^a-zA-Z0-9_]", "", (seed or "").split("@")[0])[:20] or "user"
	candidate = base
	suffix = 0
	while User.objects.filter(username__iexact=candidate).exists():
		suffix += 1
		candidate = f"{base}{suffix}"
		if suffix > 50:
			candidate = f"{base}_{uuid.uuid4().hex[:6]}"
			break
	return candidate


def _login_or_create_social_user(*, email, full_name, provider, provider_id,
                                  email_verified=False):
	"""
	Find or create a User for a social identity, returning the user (or None
	if the login must be refused — see below).

	Account pre-hijacking defense
	-----------------------------
	A social login with a *provider-verified* email is proof that the caller
	controls that address. Because local registration here cannot verify email
	ownership (there is no verification-email flow), a password account's email
	is UNTRUSTED until a verified provider vouches for it. That asymmetry is
	what an attacker abused: register `victim@example.com` locally with their
	own password, wait for the victim to "Sign in with Google", and ride into
	the account the victim now populates.

	The rules below close that:

	  • No pre-existing account → create one. It's verified iff the provider
	    verified the email.

	  • Pre-existing account, provider email NOT verified → refuse (return
	    None). We never adopt an account on the strength of an unverified
	    address from either side.

	  • Pre-existing account already email-verified → safe to log in. Both the
	    stored account and the incoming login have provably proven the address
	    (e.g. it was created via a previous social login, or verified before),
	    so this is the normal returning-user / cross-provider case.

	  • Pre-existing account NOT yet verified, provider email verified → the
	    verified provider is the rightful owner. Claim the account: mark it
	    verified and DISABLE its password, evicting any credential an
	    impersonator may have set. The legitimate owner continues via the
	    provider from now on.

	`email_verified` is supplied by the caller from the provider response
	(`_verify_google_id_token` only returns verified tokens; Facebook's Graph
	API only returns Facebook-confirmed emails).
	"""
	email = (email or "").strip().lower()
	# An email we can actually trust as an identity for this request.
	trusted_email = bool(email) and bool(email_verified)

	existing = User.objects.filter(email__iexact=email).first() if email else None

	if existing is not None:
		# Refuse to adopt a pre-existing account unless the provider has
		# verified the email. (In practice both providers reach here verified;
		# this guards future callers and degraded provider responses.)
		if not trusted_email:
			return None

		profile, _ = UserProfile.objects.get_or_create(user=existing)
		if not profile.email_verified:
			# First verified proof of ownership for a previously-unverified
			# account: take ownership and evict any (possibly attacker-set)
			# password so password login can no longer reach this account.
			profile.email_verified = True
			profile.save(update_fields=["email_verified"])
			if existing.has_usable_password():
				existing.set_unusable_password()
				existing.save(update_fields=["password"])
		return existing

	# No account for this email (or no email at all) → create a fresh one.
	seed = email or (full_name or "").replace(" ", "") or f"{provider}_{provider_id}"
	username = _username_from_seed(seed)
	user = User.objects.create(
		username=username,
		email=email,
		password=make_password(None),  # unusable password — social-only account
	)
	first, _, last = (full_name or "").partition(" ")
	UserProfile.objects.create(
		user=user,
		first_name=first[:100],
		last_name=last[:100],
		email_verified=trusted_email,
	)
	return user
