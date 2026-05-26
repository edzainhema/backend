import mimetypes

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import (
    BlockedUser, Page, PageChatMessage, PageChatMessageMedia, PageFollow,
)
from ..utils import log_activity


def _page_chat_member_or_403(user, page):
    """Return None if `user` is the page owner or a follower, else a 403."""
    if page.owner_id == user.id:
        return None
    if PageFollow.objects.filter(user=user, page=page).exists():
        return None
    return Response({"error": "Not a member of this page"}, status=403)


def _safe_build_url(request, file_field):
    """Best-effort absolute URL for a FileField; return None on any failure."""
    if not file_field:
        return None
    try:
        return request.build_absolute_uri(file_field.url)
    except Exception:
        return None


def _detect_media_type(uploaded):
    """Map an uploaded file to ('image' | 'video' | 'audio') or None."""
    mime = getattr(uploaded, "content_type", "") or (
        mimetypes.guess_type(getattr(uploaded, "name", "") or "")[0] or ""
    )
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return None


def _serialize_reply_to(reply, request):
    """Compact snippet for a quoted reply (matches the DM `reply_to` shape)."""
    if reply is None:
        return None
    r_media_url = _safe_build_url(request, reply.media)
    if not r_media_url:
        first = reply.media_items.first()
        if first and first.file:
            r_media_url = _safe_build_url(request, first.file)
    media_type = reply.media_type
    if media_type is None:
        first = reply.media_items.first()
        media_type = first.media_type if first else None
    return {
        "id":         reply.id,
        "sender_id":  reply.sender_id,
        "sender":     reply.sender.username,
        "text":       reply.text,
        "media_url":  r_media_url,
        "media_type": media_type,
        "is_deleted": False,
    }


def _serialize_page_chat_message(msg, request):
    """Shared shape for list + send so the client can append optimistically."""
    sender = msg.sender
    avatar = None
    if hasattr(sender, "userprofile") and sender.userprofile.avatar:
        try:
            avatar = request.build_absolute_uri(sender.userprofile.avatar.url)
        except Exception:
            avatar = None
    viewer = getattr(request, "user", None)
    is_mine = bool(
        viewer is not None
        and getattr(viewer, "is_authenticated", False)
        and viewer.id == sender.id
    )
    # Drop items whose URL we couldn't build (a null src crashes Image on iOS).
    media_items = []
    for item in msg.media_items.all():
        if not item.file:
            continue
        url = _safe_build_url(request, item.file)
        if not url:
            continue
        media_items.append({"url": url, "media_type": item.media_type})
    return {
        "id": msg.id,
        "page_id": msg.page_id,
        "text": msg.text,
        "created_at": msg.created_at.isoformat(),
        "is_mine": is_mine,
        "media_url": _safe_build_url(request, msg.media),
        "media_type": msg.media_type,
        "media_items": media_items,
        "reply_to": _serialize_reply_to(msg.reply_to, request),
        "sender": {
            "id": sender.id,
            "username": sender.username,
            "avatar": avatar,
        },
    }


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_page_chat(request):
    """Body: { page_id, enabled? }. Owner-only.

    If `enabled` is supplied, set `Page.chat_enabled` to that exact value so
    the call is idempotent (a retry won't desync UI and server). If it's
    omitted, fall back to flipping the current value for legacy callers.
    """
    page_id = request.data.get("page_id")
    if not page_id:
        return Response({"error": "page_id required"}, status=400)
    try:
        page_id = int(page_id)
    except (TypeError, ValueError):
        return Response({"error": "Invalid page_id"}, status=400)

    page = get_object_or_404(Page, id=page_id)
    if page.owner_id != request.user.id:
        return Response({"error": "Only the page owner can toggle chat"}, status=403)

    enabled = request.data.get("enabled", None)
    if enabled is None:
        page.chat_enabled = not page.chat_enabled
    else:
        page.chat_enabled = bool(enabled)
    page.save(update_fields=["chat_enabled"])
    return Response({
        "status": "ok",
        "page_id": page.id,
        "chat_enabled": page.chat_enabled,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_page_chat_messages(request):
    """
    GET params: page_id (req), limit (≤100), before (cursor message id).
    Returns chronological messages so the client can append to the bottom.
    """
    page_id = request.query_params.get("page_id")
    if not page_id:
        return Response({"error": "page_id required"}, status=400)
    try:
        page_id = int(page_id)
    except (TypeError, ValueError):
        return Response({"error": "Invalid page_id"}, status=400)

    raw_limit = request.query_params.get("limit", 30)
    try:
        limit = min(max(int(raw_limit), 1), 100)
    except (TypeError, ValueError):
        return Response({"error": "limit must be an integer"}, status=400)

    raw_before = request.query_params.get("before")
    before_id = None
    if raw_before is not None and raw_before != "":
        try:
            before_id = int(raw_before)
        except (TypeError, ValueError):
            return Response({"error": "before must be an integer"}, status=400)

    # `after` powers incremental polling: the client passes the largest message
    # id it already holds and we return ONLY newer messages (usually none),
    # instead of re-sending the whole recent slice on every poll. Mutually
    # exclusive with `before` (history paging); if both arrive, `after` wins.
    raw_after = request.query_params.get("after")
    after_id = None
    if raw_after is not None and raw_after != "":
        try:
            after_id = int(raw_after)
        except (TypeError, ValueError):
            return Response({"error": "after must be an integer"}, status=400)

    page = get_object_or_404(Page, id=page_id)

    # Owner can read while chat is off (so the toggle UI can still show history);
    # everyone else only sees messages while chat is enabled.
    if not page.chat_enabled and page.owner_id != request.user.id:
        return Response({"error": "Chat is not enabled for this page"}, status=403)

    blocked = _page_chat_member_or_403(request.user, page)
    if blocked is not None:
        return blocked

    qs = (
        PageChatMessage.objects
        .filter(page=page)
        .select_related("sender", "sender__userprofile", "reply_to", "reply_to__sender")
        # Prefetch grid attachments + reply chain to avoid N+1.
        .prefetch_related("media_items", "reply_to__media_items")
        .order_by("-id")  # newest-first for cheap cursor slicing
    )

    # Two-way block hide, mirroring the DM endpoint.
    blocked_pairs = set(
        BlockedUser.objects
        .involving(request.user)
        .values_list("user_id", "blocked_user_id")
    )
    flat_blocked = {uid for pair in blocked_pairs for uid in pair} - {request.user.id}
    if flat_blocked:
        qs = qs.exclude(sender_id__in=flat_blocked)

    # ── Incremental (delta) fetch ──────────────────────────────────────────
    # Newer-than-cursor messages, oldest-first so the client appends directly.
    # Bounded by `limit`; if more than `limit` messages arrived between polls
    # (rare at the client's poll cadence) the next poll uses an advanced cursor
    # and picks up the remainder, so nothing is skipped or duplicated.
    if after_id is not None:
        delta_qs = qs.filter(id__gt=after_id).order_by("id")
        rows = list(delta_qs[:limit])
        serialized = [_serialize_page_chat_message(m, request) for m in rows]
        return Response({
            "messages":  serialized,
            "results":   serialized,
            # True only when more *newer* messages remain beyond this capped batch.
            "has_more":  (
                delta_qs.filter(id__gt=rows[-1].id).exists() if rows else False
            ),
            "oldest_id": rows[0].id if rows else None,
            "latest_id": rows[-1].id if rows else after_id,
        })

    if before_id is not None:
        qs = qs.filter(id__lt=before_id)

    rows = list(qs[:limit])
    rows.reverse()  # chronological for the client

    has_more = qs.filter(id__lt=rows[0].id).exists() if rows else False

    serialized = [_serialize_page_chat_message(m, request) for m in rows]
    return Response({
        # `messages` is canonical; `results` is kept as a legacy alias.
        "messages":  serialized,
        "results":   serialized,
        "has_more":  has_more,
        "oldest_id": rows[0].id if rows else None,
    })


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
@permission_classes([IsAuthenticated])
def send_page_chat_message(request):
    """
    Multipart or JSON:
      page_id (req), text (optional w/ media),
      media (single file) OR media_0, media_1, ... (multi-attachment).

    Owner or follower only, chat must be enabled. Returns the created message
    in the same shape as list_page_chat_messages.
    """
    page_id = request.data.get("page_id")
    text = request.data.get("text") or ""
    reply_to_id = request.data.get("reply_to_id")
    if not page_id:
        return Response({"error": "page_id required"}, status=400)
    try:
        page_id = int(page_id)
    except (TypeError, ValueError):
        return Response({"error": "Invalid page_id"}, status=400)

    text = str(text).strip()
    if len(text) > 4000:
        return Response({"error": "text is too long (max 4000 chars)"}, status=400)

    # Prefer indexed media_0,1,2,... ; fall back to single `media`.
    single_media = request.FILES.get("media")
    indexed_media = []
    i = 0
    while True:
        f = request.FILES.get(f"media_{i}")
        if f is None:
            break
        indexed_media.append(f)
        i += 1

    if indexed_media:
        media_files = indexed_media
    elif single_media:
        media_files = [single_media]
    else:
        media_files = []

    if not text and not media_files:
        return Response({"error": "Message empty"}, status=400)

    typed_files = []
    for f in media_files:
        t = _detect_media_type(f)
        if t is None:
            return Response(
                {"error": f"Unsupported media type: {getattr(f, 'name', 'upload')}"},
                status=400,
            )
        typed_files.append((f, t))

    page = get_object_or_404(Page, id=page_id)
    if not page.chat_enabled:
        return Response({"error": "Chat is not enabled for this page"}, status=403)

    blocked = _page_chat_member_or_403(request.user, page)
    if blocked is not None:
        return blocked

    # The page owner can moderate by blocking individuals: a user the owner
    # has blocked may not post into the page chat at all, even if they're
    # still a follower. This mirrors the read-side block filtering.
    if BlockedUser.objects.filter(
        user_id=page.owner_id, blocked_user_id=request.user.id
    ).exists():
        return Response({"error": "You cannot post in this chat"}, status=403)

    # Resolve the reply target (scoped to this page; unknown id -> no reply).
    reply_to_obj = None
    if reply_to_id:
        try:
            reply_to_obj = PageChatMessage.objects.get(id=int(reply_to_id), page=page)
        except (PageChatMessage.DoesNotExist, TypeError, ValueError):
            reply_to_obj = None

    # Save atomically so a partial fan-out can't leave a ghost message.
    with transaction.atomic():
        if len(typed_files) == 1:
            legacy_file, legacy_type = typed_files[0]
            msg = PageChatMessage.objects.create(
                page=page, sender=request.user, text=text,
                media=legacy_file, media_type=legacy_type,
                reply_to=reply_to_obj,
            )
            # Mirror into PageChatMessageMedia by reference to avoid
            # re-uploading the file (FileSystemStorage would duplicate it).
            mm = PageChatMessageMedia(message=msg, media_type=legacy_type, order=0)
            mm.file.name = msg.media.name
            mm.save()
        elif len(typed_files) > 1:
            msg = PageChatMessage.objects.create(
                page=page, sender=request.user, text=text,
                reply_to=reply_to_obj,
            )
            for order, (f, t) in enumerate(typed_files):
                row = PageChatMessageMedia.objects.create(
                    message=msg, file=f, media_type=t, order=order,
                )
                # Keep first attachment in legacy fields for old clients.
                if order == 0:
                    msg.media.name = row.file.name
                    msg.media_type = t
                    msg.save(update_fields=["media", "media_type"])
        else:
            msg = PageChatMessage.objects.create(
                page=page, sender=request.user, text=text,
                reply_to=reply_to_obj,
            )

    try:
        log_activity(request.user, "page_chat_message", page_id=page.id)
    except Exception:
        pass

    data = _serialize_page_chat_message(msg, request)

    # Broadcast to the page's WebSocket group (FE-2) so every viewer receives
    # the message in real time instead of polling. Best-effort: a channel-layer
    # hiccup must never fail the send — the message is already persisted, and
    # clients reconnect + catch up via list_page_chat_messages. is_mine in the
    # payload reflects the SENDER; recipients recompute it client-side.
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if layer is not None:
            async_to_sync(layer.group_send)(
                f"pagechat_{page.id}",
                {"type": "broadcast", "payload": {"type": "message.new", "message": data}},
            )
    except Exception:
        pass

    return Response(data, status=201)
# .............................................................