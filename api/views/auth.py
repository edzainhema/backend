

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User

from rest_framework.decorators import api_view
from rest_framework.response import Response



from ..models import (
    UserProfile,
)
from ..services.auth_helpers import (
    _find_user_by_identifier, _issue_tokens, _login_or_create_social_user,
    _looks_like_email, _looks_like_phone, _normalize_phone, _verify_facebook_access_token,
    _verify_google_id_token,
)


@api_view(['POST'])
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

	user = User.objects.create(
		username=username,
		email=email,
		password=make_password(password),
	)

	UserProfile.objects.create(user=user, phone_number=phone)

	tokens = _issue_tokens(user)
	return Response({
		"message": "User registered successfully",
		**tokens,
	})


@api_view(['POST'])
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
