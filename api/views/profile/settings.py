"""Profile mutations: update settings, update avatar."""


from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import UserProfile
from ...serializers import UserProfileSerializer
from ...services.auth_helpers import (
    _looks_like_email, _looks_like_phone, _normalize_phone,
)
from ...services.email_verification import send_verification_email
from ...services.media import safe_image_filename, validate_image_upload


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_profile_settings(request):
    user = request.user
    profile = getattr(user, "userprofile", None)

    if not profile:
        return Response(
            {"error": "Profile not found"},
            status=404
        )

    # --------------------------------------------------
    # 🔐 PRIVACY TOGGLES
    # --------------------------------------------------

    if "is_private" in request.data:
        profile.is_private = bool(
            request.data.get("is_private")
        )

    if "memories_public" in request.data:
        profile.memories_public = bool(
            request.data.get("memories_public")
        )

    # --------------------------------------------------
    # 👤 PROFILE INFO
    # --------------------------------------------------

    if "first_name" in request.data:
        profile.first_name = request.data.get(
            "first_name", ""
        ).strip()

    if "last_name" in request.data:
        profile.last_name = request.data.get(
            "last_name", ""
        ).strip()

    # --------------------------------------------------
    # 📞 PHONE NUMBER (audit H4)
    # --------------------------------------------------
    # Phone is a login identifier (`_find_user_by_identifier` resolves it),
    # so updates must mirror the rules `register_user` enforces:
    #   1. validate the format -- otherwise an invalid string lives in the
    #      profile and breaks `_find_user_by_identifier`;
    #   2. normalise via `_normalize_phone` so "+1 555-123-4567" and
    #      "+15551234567" can't both exist as two distinct rows pointing
    #      at the same human;
    #   3. uniqueness-check against OTHER users so a fat-fingered or
    #      malicious update can't claim another account's phone (the
    #      collision would otherwise let the wrong user sign in via phone).
    # An empty input clears the phone. The unique constraint added by
    # migration 0099 is the DB-level backstop -- the catch below maps
    # the resulting IntegrityError to a 400.
    if "phone_number" in request.data:
        raw_phone = (request.data.get("phone_number") or "").strip()
        if raw_phone:
            if not _looks_like_phone(raw_phone):
                return Response({"error": "Invalid phone number"}, status=400)
            normalized = _normalize_phone(raw_phone)
            if UserProfile.objects.exclude(
                user=user
            ).filter(phone_number=normalized).exists():
                return Response(
                    {"error": "Phone number already in use"}, status=400,
                )
            profile.phone_number = normalized
        else:
            profile.phone_number = ""

    if "bio" in request.data:
        profile.bio = request.data.get(
            "bio", ""
        ).strip()

    # --------------------------------------------------
    # ✉️ EMAIL
    # --------------------------------------------------

    # Track whether the email is actually changing so we can clear
    # `email_verified` and trigger a fresh verification mail AFTER
    # the save (M5). Reading user.email pre-mutation captures the
    # original value cleanly.
    email_changed_to = None
    if "email" in request.data:
        email = (request.data.get("email") or "").strip()

        # Match registration's rules so the two paths can't disagree: validate
        # the format (registration uses _looks_like_email) and check uniqueness
        # case-INSENSITIVELY. Store lowercased — the way register_user and
        # social login already do — so "Bob@x.com" and "bob@x.com" can never
        # become two different accounts (M3). An empty value clears the email.
        if email:
            if not _looks_like_email(email):
                return Response(
                    {"error": "Invalid email address"},
                    status=400
                )
            email = email.lower()
            if User.objects.exclude(
                id=user.id
            ).filter(email__iexact=email).exists():
                return Response(
                    {"error": "Email already in use"},
                    status=400
                )

        if email != (user.email or ""):
            email_changed_to = email
            # M5: a fresh email means the old `email_verified` flag no
            # longer applies. Clear it BEFORE the save so we never have
            # a window where `user.email = new` + `profile.email_verified
            # = True (stale)` is visible -- any reader could mistakenly
            # treat the new address as verified.
            profile.email_verified = False

        user.email = email

    # --------------------------------------------------
    # 🧑 USERNAME (12 MONTH LIMIT)
    # --------------------------------------------------

    if "username" in request.data:
        new_username = (request.data.get("username") or "").strip()

        if new_username != user.username:
            # Registration requires a non-empty username; enforce the same here
            # so an update can't blank it out.
            if not new_username:
                return Response(
                    {"error": "Username cannot be empty"},
                    status=400
                )

            if not profile.can_change_username():
                return Response(
                    {
                        "error": (
                            "Username can only be "
                            "changed once every 12 months"
                        )
                    },
                    status=403
                )

            # Uniqueness must be case-INSENSITIVE to match registration (which
            # uses username__iexact); otherwise "Bob" and "bob" could coexist
            # and @mentions — which resolve case-insensitively — would notify
            # both (M3). The username's own case is preserved for display,
            # exactly as registration stores it.
            if User.objects.exclude(
                id=user.id
            ).filter(username__iexact=new_username).exists():
                return Response(
                    {"error": "Username already taken"},
                    status=400
                )

            # All checks passed — apply the new username and start the
            # 12-month clock so can_change_username() gates the next change.
            user.username = new_username
            profile.last_username_change = timezone.now()

    # --------------------------------------------------
    # 💾 PERSIST + RETURN THE UPDATED PROFILE
    # --------------------------------------------------
    # H3: wrap both saves in atomic + IntegrityError catch. The
    # application-layer pre-checks above (email / username / phone)
    # are TOCTOU-racy with the saves; another request can grab the
    # identifier in the window between check and save. The DB
    # constraints from migration 0099 make the losing request raise
    # IntegrityError -- without this catch that becomes a 500. The
    # atomic() also keeps `profile.save()` and `user.save()` together:
    # if the second fails, the first rolls back so we don't half-save
    # the update.
    try:
        with transaction.atomic():
            profile.save()
            user.save()
    except IntegrityError:
        # Re-check to identify which field collided so the client gets
        # a useful message instead of a generic 500.
        if User.objects.exclude(
            id=user.id
        ).filter(username__iexact=user.username).exists():
            return Response({"error": "Username already taken"}, status=400)
        if user.email and User.objects.exclude(
            id=user.id
        ).filter(email__iexact=user.email).exists():
            return Response({"error": "Email already in use"}, status=400)
        if profile.phone_number and UserProfile.objects.exclude(
            user=user
        ).filter(phone_number=profile.phone_number).exists():
            return Response(
                {"error": "Phone number already in use"}, status=400,
            )
        return Response(
            {"error": "Could not save changes, please try again."},
            status=409,
        )

    # M5: now that the new email is committed AND `email_verified` is
    # already cleared (above), send the verification mail to the new
    # address. AFTER the atomic block so a flaky SES can't roll back
    # an otherwise-successful settings update. Best-effort -- the
    # service silently no-ops on no-email-on-file / inactive / already
    # verified branches, and we swallow any send failure.
    if email_changed_to:
        try:
            send_verification_email(user)
        except Exception:
            pass

    return Response(
        UserProfileSerializer(profile, context={"request": request}).data
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_profile_avatar(request):
    """Replace the current user's avatar (multipart field "avatar").

    POSTed by Profile/hooks/useAvatarUpload to /auth/profile/avatar/; returns
    {"avatar": <absolute url>}, the shape the client splices into the profile
    blob. The image is validated via the hardened validate_image_upload path
    BEFORE it's saved: ImageField.save() does NOT run image validation, so
    without this an arbitrary client file would be written straight under
    media/avatars (the M2 finding; see UPLOAD_BUG_AUDIT.md).
    """
    profile = getattr(request.user, "userprofile", None)
    if not profile:
        return Response({"error": "Profile not found"}, status=404)

    avatar = request.FILES.get("avatar")
    if not avatar:
        return Response({"error": "No image provided"}, status=400)

    try:
        validate_image_upload(avatar)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=400)

    # M6: persist with a SERVER-derived filename instead of the
    # client-supplied one. Without this, FileField stored whatever
    # the multipart field carried -- so avatar URLs ended up like
    # `/media/avatars/<whatever-the-client-typed>.png`, leaking
    # arbitrary client strings into the public URL. The bytes were
    # already validated upstream; we just no longer trust the *name*.
    # `safe_image_filename` derives the extension from the file's
    # actual bytes via a Pillow probe, so `.png` always means PNG.
    safe_name = safe_image_filename(
        avatar, f"profile_{request.user.id}_avatar",
    )
    profile.avatar.save(safe_name, avatar, save=True)

    avatar_url = (
        request.build_absolute_uri(profile.avatar.url)
        if profile.avatar else None
    )
    return Response({"avatar": avatar_url})

