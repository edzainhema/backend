"""Contact matching: find existing accounts among a viewer's phone/email
contacts so they can follow people they already know ("Find friends")."""

import re

from django.contrib.auth.models import User
from django.db.models import Q
from django.db.models.functions import Lower
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Follow, FollowRequest, UserProfile

# Bound how much a single request can submit, so a huge address book can't be
# turned into an expensive query / data-exfiltration probe.
MAX_CONTACTS = 2000


def _phone_variants(raw):
    """Candidate stored-formats a contact phone might be saved as.

    Phone numbers are stored as free-form text (UserProfile.phone_number) and
    contacts arrive in every format (+1 (415) 555-1234, 415-555-1234, ...). We
    can't normalize the column cheaply across databases, so we expand each
    contact number into the handful of forms it's commonly stored as and match
    those with `phone_number__in`. US-centric on the +1 / 10-digit forms, with
    the raw + digits-only string as a fallback for everything else.
    """
    out = set()
    if not isinstance(raw, str):
        return out
    raw = raw.strip()
    if raw:
        out.add(raw)
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return out
    out.add(digits)
    if len(digits) >= 10:
        last10 = digits[-10:]
        out.add(last10)
        out.add("1" + last10)
        out.add("+1" + last10)
    return out


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def match_contacts(request):
    """
    Find existing accounts among the viewer's contacts.

    POST /auth/contacts/match/
      { "phones": ["+1 415 555-1234", ...], "emails": ["a@b.com", ...] }

    Matches by exact (case-insensitive) email on User.email and by phone on
    UserProfile.phone_number (see _phone_variants). Excludes the viewer and
    anyone in a block relationship with them. Returns user cards with the
    viewer's current follow state so the list renders Follow / Requested /
    Following without a second call.

    Response: { "results": [ {id, username, avatar, is_private,
                              is_following, has_requested_follow}, ... ] }
    """
    phones = request.data.get("phones") or []
    emails = request.data.get("emails") or []
    if not isinstance(phones, list):
        phones = []
    if not isinstance(emails, list):
        emails = []
    phones = phones[:MAX_CONTACTS]
    emails = emails[:MAX_CONTACTS]

    email_set = {
        e.strip().lower()
        for e in emails
        if isinstance(e, str) and e.strip()
    }
    phone_set = set()
    for p in phones:
        phone_set |= _phone_variants(p)

    user_ids = set()
    if email_set:
        user_ids.update(
            User.objects.annotate(_el=Lower("email"))
            .filter(_el__in=email_set)
            .values_list("id", flat=True)
        )
    if phone_set:
        user_ids.update(
            UserProfile.objects.filter(phone_number__in=phone_set)
            .values_list("user_id", flat=True)
        )

    user_ids.discard(request.user.id)
    if not user_ids:
        return Response({"results": []})

    # Drop anyone in a block relationship with the viewer (either direction).
    blocked_ids = set()
    for b in BlockedUser.objects.filter(
        Q(user=request.user, blocked_user_id__in=user_ids)
        | Q(blocked_user=request.user, user_id__in=user_ids)
    ):
        blocked_ids.add(
            b.blocked_user_id if b.user_id == request.user.id else b.user_id
        )
    user_ids -= blocked_ids
    if not user_ids:
        return Response({"results": []})

    following_ids = set(
        Follow.objects.filter(
            follower=request.user, following_id__in=user_ids
        ).values_list("following_id", flat=True)
    )
    requested_ids = set(
        FollowRequest.objects.filter(
            requester=request.user, target_id__in=user_ids
        ).values_list("target_id", flat=True)
    )

    users = (
        User.objects.filter(id__in=user_ids)
        .select_related("userprofile")
        .order_by("username", "id")
    )

    results = []
    for u in users:
        up = getattr(u, "userprofile", None)
        results.append({
            "id": u.id,
            "username": u.username,
            "avatar": (
                request.build_absolute_uri(up.avatar.url)
                if up and up.avatar
                else None
            ),
            "is_private": up.is_private if up else False,
            "is_following": u.id in following_ids,
            "has_requested_follow": u.id in requested_ids,
        })

    return Response({"results": results})
