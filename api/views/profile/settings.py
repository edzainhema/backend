"""Profile mutations: update settings, update avatar."""


from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...serializers import UserProfileSerializer
from ...services.auth_helpers import _looks_like_email
from ...services.media import validate_image_upload


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

    if "phone_number" in request.data:
        profile.phone_number = request.data.get(
            "phone_number", ""
        ).strip()

    if "bio" in request.data:
        profile.bio = request.data.get(
            "bio", ""
        ).strip()

    # --------------------------------------------------
    # ✉️ EMAIL
    # --------------------------------------------------

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
    profile.save()
    user.save()

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

    profile.avatar = avatar
    profile.save(update_fields=["avatar"])

    avatar_url = (
        request.build_absolute_uri(profile.avatar.url)
        if profile.avatar else None
    )
    return Response({"avatar": avatar_url})

