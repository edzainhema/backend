

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
	Re-registering the same (user, token) pair bumps `last_seen` (marking the
	row live) without creating a duplicate.

	H3 — cross-account push-leak containment. An FCM token is unique per app
	INSTALL, not per account, so several accounts on one phone share a token.
	If account A logs out but its client `unregister-device` call never lands
	(crash / offline / force-quit), a stale (A, token) row would keep
	delivering A's notifications to whoever now holds the phone. We deliberately
	do NOT blind-delete other accounts' rows for this token here: the token
	alone can't distinguish "A logged out" from "A and B are both signed in,"
	so a delete would break the legitimate multi-account case. Instead, every
	live registration bumps `last_seen`; a logged-out account stops registering,
	so its row goes stale and `prune_stale_devices` reaps it, while
	still-signed-in accounts keep refreshing and survive. For this to hold the
	client MUST:
	  • call this endpoint for EVERY signed-in account on each app foreground
	    (not just the active one), so live rows stay fresh; and
	  • call /auth/unregister-device/ on logout / account-switch — the fast
	    path; the reaper is only the backstop for when that call fails.
	FCM's own dead-token pruning (UnregisteredError etc. in services/push.py)
	is the third layer.
	"""
	token = request.data.get('token')

	if not token:
		return Response({"error": "FCM token required"}, status=400)

	# Key the upsert by (user, token), not user alone. The Device model's
	# unique_together = ('user', 'token') constraint enforces this at the
	# DB layer (see migration 0069_device_unique_together). Bump `last_seen`
	# on every (re-)register so the row reads as live — this is the freshness
	# signal `prune_stale_devices` reaps against (H3). Passing it via
	# `defaults` means an existing row is updated, not just matched (the old
	# code passed no defaults, so a repeat register was a true no-op — the
	# docstring used to overclaim it touched `created_at`).
	Device.objects.update_or_create(
		user=request.user,
		token=token,
		defaults={"last_seen": timezone.now()},
	)

	return Response({"status": "device registered"})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def unregister_device(request):
	"""Drop the calling user's Device row(s) for push.

	Called from the frontend signOut() flow so that, on a multi-account
	device, signing out of account A stops A's pushes on THIS device while
	leaving the other accounts' Device rows intact. That requires the device's
	FCM `token`, which the client passes.

	L5: a token is REQUIRED by default. The endpoint used to treat a missing
	token as "delete ALL of this user's Device rows" — so a signOut() that
	forgot to pass the token would silently kill push on the user's OTHER
	phones too. That nuke is now opt-in only: pass `all_devices=true` to log
	out of push everywhere. A call with neither `token` nor `all_devices` is a
	400 (symmetric with register_device, which already requires a token),
	turning a silent footgun into an explicit, intentional action.
	"""
	token = request.data.get('token')
	all_devices_raw = request.data.get('all_devices')
	all_devices = (
		all_devices_raw is True
		or str(all_devices_raw).strip().lower() in ('true', '1', 'yes')
	)

	if not token and not all_devices:
		return Response(
			{"error": "FCM token required (or pass all_devices=true to log out of push everywhere)"},
			status=400,
		)

	qs = Device.objects.filter(user=request.user)
	if token:
		# Drop only THIS device's row (the common signOut case). When both
		# token and all_devices are sent, token wins — it's the more specific,
		# safer scope.
		qs = qs.filter(token=token)
	# else: all_devices=true with no token -> delete every row for this user.
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
