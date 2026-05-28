"""Page invites: list sent, search invitees, invite, cancel, and respond."""


from django.contrib.auth.models import User
from django.core.cache import cache
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Notification, Page, PageFollow, PageInvite
from ...services.push import push_to_user
from ...services.pagination import decode_cursor, encode_cursor

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_sent_page_invites(request):
    """
    Pending invites already sent for a page, newest first. Only the page owner
    can call this. Keyset/cursor pagination ordered by (-created_at, -id) on the
    same contract as the rest of the people lists.

    GET /pages/invite/sent/?page_id=<id>

    GET params:
      page_id — required
      limit   — page size (default 20, capped at 50)
      cursor  — opaque token from the previous page's `next_cursor`

    Response: { "results": [...], "has_more": bool, "next_cursor": str|null }
    """
    user = request.user
    page_id = request.query_params.get("page_id")

    if not page_id:
        return Response({"error": "page_id required"}, status=400)

    page = get_object_or_404(Page, id=page_id)

    if page.owner != user:
        return Response({"error": "Not authorized"}, status=403)

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    qs = (
        PageInvite.objects
        .filter(page=page)
        .select_related("invited_user__userprofile")
        .order_by("-created_at", "-id")
    )

    # Keyset: rows strictly older than the cursor. Compound comparison keeps
    # ordering total/stable when two invites share a timestamp.
    cursor = decode_cursor(request.query_params.get("cursor"))
    last_created = parse_datetime(cursor["created_at"]) if cursor.get("created_at") else None
    last_id = cursor.get("id")
    if last_created is not None and last_id is not None:
        qs = qs.filter(
            Q(created_at__lt=last_created)
            | Q(created_at=last_created, id__lt=last_id)
        )

    # Fetch one extra row to detect `has_more` without a second COUNT query.
    invites = list(qs[: limit + 1])
    has_more = len(invites) > limit
    invites = invites[:limit]

    results = []
    for inv in invites:
        u = inv.invited_user
        up = getattr(u, "userprofile", None)
        results.append({
            "id": u.id,
            "username": u.username,
            "avatar": (
                request.build_absolute_uri(up.avatar.url)
                if up and up.avatar
                else None
            ),
        })

    next_cursor = None
    if has_more and invites:
        last = invites[-1]
        next_cursor = encode_cursor({
            "created_at": last.created_at.isoformat(),
            "id": last.id,
        })

    return Response({
        "results": results,
        "has_more": has_more,
        "next_cursor": next_cursor,
    })



@api_view(["GET"])
@permission_classes([IsAuthenticated])
def search_users_for_page_invite(request):
    """
    Admin searches for users to invite to their page.
    Excludes: already following, already invited, self.
    GET /pages/invite/search/?page_id=<id>&q=<query>
    """
    user = request.user
    page_id = request.query_params.get("page_id")
    q = request.query_params.get("q", "").strip()
 
    if not page_id:
        return Response({"error": "page_id required"}, status=400)
 
    page = get_object_or_404(Page, id=page_id)
 
    if page.owner != user:
        return Response({"error": "Not authorized"}, status=403)
 
    if not q:
        return Response({"results": [], "has_more": False, "next_offset": None})

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    # IDs to exclude
    already_following = PageFollow.objects.filter(page=page).values_list("user_id", flat=True)
    already_invited_ids = set(
        PageInvite.objects.filter(page=page).values_list("invited_user_id", flat=True)
    )

    users = (
        User.objects
        .filter(username__icontains=q)
        .exclude(id=user.id)
        .exclude(id__in=already_following)
        .select_related("userprofile")
        .order_by("username", "id")
    )

    # Offset window: fetch one extra row to detect `has_more` without a COUNT.
    window = list(users[offset : offset + limit + 1])
    has_more = len(window) > limit
    window = window[:limit]

    data = []
    for u in window:
        up = getattr(u, "userprofile", None)
        data.append({
            "id": u.id,
            "username": u.username,
            "avatar": (
                request.build_absolute_uri(up.avatar.url)
                if up and up.avatar
                else None
            ),
            "invited": u.id in already_invited_ids,
        })

    return Response({
        "results": data,
        "has_more": has_more,
        "next_offset": (offset + limit) if has_more else None,
    })



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def invite_to_page(request):
    """
    Admin sends a page invite to a user.
    POST /pages/invite/  { page_id, user_id }
    Creates a PageInvite + page_invite Notification.
    """
    admin = request.user
    page_id = request.data.get("page_id")
    target_user_id = request.data.get("user_id")
 
    if not page_id or not target_user_id:
        return Response({"error": "page_id and user_id required"}, status=400)
 
    page = get_object_or_404(Page, id=page_id)
 
    if page.owner != admin:
        return Response({"error": "Not authorized"}, status=403)
 
    target = get_object_or_404(User, id=target_user_id)
 
    # Already following?
    if PageFollow.objects.filter(page=page, user=target).exists():
        return Response({"error": "User already follows this page"}, status=400)
 
    # Create invite (idempotent — ignore if already exists)
    invite, created = PageInvite.objects.get_or_create(
        page=page,
        invited_user=target,
        defaults={"invited_by": admin},
    )
 
    if not created:
        return Response({"status": "already_invited"})
 
    # Notify the invited user — store page so cancel_page_invite can filter on it.
    Notification.objects.create(
        recipient=target,
        actor=admin,
        notification_type="page_invite",
        page=page,
    )
 
    # ── Push notification (best-effort) ──────────────────────────────────────
    # NOTE: previously called send_push_notification(target, ...) which
    # passed a User object as the `tokens` arg — silently failed inside the
    # try/except for as long as the call site existed. push_to_user takes
    # the recipient directly and handles the token lookup.
    try:
        push_to_user(
            target,
            title=page.name,
            body=f"{admin.username} invited you to follow {page.name}",
            extra_data={
                "type": "page_invite",
                "page_id": page.id,
                "actor_id": admin.id,
            },
        )
    except Exception:
        pass
 
    return Response({"status": "invited"})



@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def cancel_page_invite(request):
    """
    Admin cancels a previously sent invite.
    DELETE /pages/invite/cancel/  { page_id, user_id }
    """
    admin = request.user
    page_id = request.data.get("page_id")
    target_user_id = request.data.get("user_id")

    if not page_id or not target_user_id:
        return Response({"error": "page_id and user_id required"}, status=400)

    page = get_object_or_404(Page, id=page_id)

    if page.owner != admin:
        return Response({"error": "Not authorized"}, status=403)

    deleted, _ = PageInvite.objects.filter(
        page=page,
        invited_user_id=target_user_id,
    ).delete()

    if deleted:
        # Remove the pending notification so recipient's feed stays clean
        Notification.objects.filter(
            recipient_id=target_user_id,
            actor=admin,
            notification_type="page_invite",
            page=page,
        ).delete()
        return Response({"status": "cancelled"})
    return Response({"error": "Invite not found"}, status=404)



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def respond_to_page_invite(request):
    """
    Invited user accepts or declines.
    POST /pages/invite/respond/  { invite_id, action: "accept"|"decline" }
    """
    user   = request.user
    invite_id = request.data.get("invite_id")
    action    = request.data.get("action")  # "accept" or "decline"
 
    if not invite_id or action not in ("accept", "decline"):
        return Response({"error": "invite_id and action ('accept'|'decline') required"}, status=400)
 
    invite = get_object_or_404(PageInvite, id=invite_id, invited_user=user)

    # Capture FK targets before delete() so the notification scope below is
    # not affected by the cascading row removal.
    invite_page = invite.page
    invite_actor = invite.invited_by

    if action == "accept":
        # 🚫 BLOCK CHECK — the page owner may have blocked the invitee
        # (or vice versa) since the invite was sent. In that case accept
        # is denied, but we still tear down the invite row + notification
        # so the user isn't left with a dead entry they can't action.
        if invite_page.owner_id != user.id and BlockedUser.objects.between(
            invite_page.owner, user
        ).exists():
            invite.delete()
            Notification.objects.filter(
                recipient=user,
                page=invite_page,
                notification_type="page_invite",
            ).delete()
            return Response({"error": "Not allowed"}, status=403)

        # Create a PageFollow (idempotent)
        PageFollow.objects.get_or_create(page=invite_page, user=user)
        # The user's followed_pages just changed; invalidate the same
        # caches that toggle_page_follow / approve_page_follow_request do.
        cache.delete(f"feed_ctx:{user.id}")
        cache.delete(f"suggested_feed_scores:{user.id}")

    # Delete the invite regardless of action
    invite.delete()

    # Mark THIS invite's notification read. Without the page filter, accepting
    # one invite from admin X would also mark every other outstanding
    # page_invite notification from X (across all of X's pages) as read,
    # making the badge under-report until a fresh invite arrived.
    Notification.objects.filter(
        recipient=user,
        actor=invite_actor,
        notification_type="page_invite",
        page=invite_page,
    ).update(is_read=True)

    # `.update()` bypasses post_save, so invalidate the badge cache explicitly.
    from ...services.notification_cache import invalidate_unread_count_cache
    invalidate_unread_count_cache(user.id)

    return Response({"status": action + "ed"})
