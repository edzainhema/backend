

from django.utils import timezone

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response



from ..models import (
    Device, UserProfile,
)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def register_device(request):
	"""Register the calling user for push on this device (additive).

	Multi-account, multi-device model: a single physical device can have
	many accounts logged in, AND a single account can be logged in on many
	devices. The row key is (user, token) — one row per physical device per
	account. The previous version keyed by user alone, which meant logging
	into account A on a second phone silently overwrote the first phone's
	token and the first phone stopped getting pushes with no warning.

	Idempotent: safe to call on every login and on every app foreground.
	Re-registering the same (user, token) pair updates `created_at` via
	update_or_create's no-op defaults path but does not create a duplicate.

	When a user explicitly logs OUT, the frontend should call
	/auth/unregister-device/ to drop ONLY that (user, token) row, leaving
	the other accounts on this device — and the same account on other
	devices — untouched.
	"""
	token = request.data.get('token')

	if not token:
		return Response({"error": "FCM token required"}, status=400)

	# Key the upsert by (user, token), not user alone. The Device model's
	# unique_together = ('user', 'token') constraint enforces this at the
	# DB layer (see migration 0069_device_unique_together).
	Device.objects.update_or_create(
		user=request.user,
		token=token,
	)

	return Response({"status": "device registered"})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def unregister_device(request):
	"""Drop the calling user's Device row for the given FCM token.

	Called from the frontend signOut() flow so that, on a multi-account
	device, signing out of account A stops A's pushes on this device while
	leaving the other accounts' Device rows intact.

	If `token` is omitted, falls back to deleting ALL Device rows for the
	calling user — useful for "log me out of all my devices" flows.
	"""
	token = request.data.get('token')

	qs = Device.objects.filter(user=request.user)
	if token:
		qs = qs.filter(token=token)
	deleted, _ = qs.delete()

	return Response({"status": "device unregistered", "deleted": deleted})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def update_user_location(request):
	"""Record the calling user's most recent device location.

	The frontend calls this on first launch (after the user grants the
	location permission) and on every cold start / foreground refresh
	thereafter. Used to rank local content in the home feed / pages
	search. The latitude/longitude are intentionally NOT exposed back to
	any other user — see serializers.py, where they're omitted from the
	public profile shape.

	Coordinates outside the legal ranges are rejected outright rather
	than silently clamped — that almost always means the client sent
	garbage (sensor blip, mocked location, accidental parse error) and
	we'd rather log it than store a fix that'll confuse ranking later.
	"""
	lat_raw = request.data.get('latitude')
	lng_raw = request.data.get('longitude')
	acc_raw = request.data.get('accuracy')

	if lat_raw is None or lng_raw is None:
		return Response(
			{"error": "latitude and longitude required"},
			status=400,
		)

	try:
		latitude = float(lat_raw)
		longitude = float(lng_raw)
	except (TypeError, ValueError):
		return Response(
			{"error": "latitude/longitude must be numbers"},
			status=400,
		)

	if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
		return Response(
			{"error": "latitude/longitude out of range"},
			status=400,
		)

	accuracy = None
	if acc_raw is not None:
		try:
			accuracy = float(acc_raw)
			# Negative accuracy is nonsense; some Android emulators report
			# 0.0 which is also nonsense but harmless. Just discard the
			# negative case rather than 400'ing the whole call.
			if accuracy < 0:
				accuracy = None
		except (TypeError, ValueError):
			accuracy = None

	# UserProfile is created lazily for some sign-up paths; use
	# get_or_create so this endpoint also seeds the row if needed.
	profile, _ = UserProfile.objects.get_or_create(user=request.user)
	profile.latitude = latitude
	profile.longitude = longitude
	profile.location_accuracy_m = accuracy
	profile.location_updated_at = timezone.now()
	profile.save(update_fields=[
		"latitude",
		"longitude",
		"location_accuracy_m",
		"location_updated_at",
	])

	return Response({"status": "location updated"})
