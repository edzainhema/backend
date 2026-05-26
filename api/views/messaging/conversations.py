"""Conversation lifecycle: start (DM/group), list, delete, rename, and user search."""


from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.models import User
from django.db import models, transaction
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Conversation, ConversationHidden, Follow, Message, MessageMedia
from ...serializers import ConversationSerializer

CONVERSATION_NAME_MAX_LEN = 100



@api_view(['POST'])
@permission_classes([IsAuthenticated])
def start_conversation(request):
    user_id = request.data.get('user_id')
    if not user_id:
        return Response({"error": "user_id required"}, status=400)
    try:
        other_user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)

    # 🔒 BLOCK CHECK (BOTH DIRECTIONS)
    if BlockedUser.objects.between(request.user, other_user).exists():
        return Response({"error": "Not allowed"}, status=403)

    # 🔁 FIND-OR-CREATE 1-TO-1 CONVERSATION
    # Lock both user rows in deterministic id order so two concurrent
    # requests for the same pair serialize here instead of both creating
    # a fresh Conversation row.
    with transaction.atomic():
        list(
            User.objects
            .select_for_update()
            .filter(id__in=sorted({request.user.id, other_user.id}))
            .order_by('id')
        )

        # ⚠️  Why this is in TWO queries:
        # Chaining `.filter(participants=A).filter(participants=B)` adds
        # separate JOINs on the M2M table — each with its own WHERE
        # constraint on user_id. If we then `.annotate(Count("participants"))`
        # in the same queryset, Django can reuse one of those constrained
        # JOINs for the aggregation, which makes the count reflect the
        # *filtered* participant set (effectively 1 or 2) instead of the
        # conversation's true participant count. That made `num_participants=2`
        # silently fail to match real 1-to-1 conversations between
        # request.user and other_user, so we always fell through to
        # `Conversation.objects.create()` and produced a duplicate convo
        # every time. Evaluating the candidate IDs into a Python list
        # before the count annotation isolates the aggregation, so the
        # count is computed against all participants of those candidates.
        candidate_ids = list(
            Conversation.objects
            .filter(participants=request.user)
            .filter(participants=other_user)
            .values_list("id", flat=True)
        )
        convo = (
            Conversation.objects
            .filter(id__in=candidate_ids)
            .annotate(num_participants=models.Count("participants"))
            .filter(num_participants=2)
            .first()
        )

        if not convo:
            convo = Conversation.objects.create()
            convo.participants.add(request.user, other_user)

        # If the convo was previously soft-hidden by this user (via
        # ConversationHidden), un-hide it so it shows up in their inbox
        # again — otherwise clicking Message looks like nothing happened
        # because the existing thread is still hidden.
        ConversationHidden.objects.filter(
            user=request.user, conversation=convo
        ).delete()

    return Response({"conversation_id": convo.id})



@api_view(['POST'])
@permission_classes([IsAuthenticated])
def start_group_conversation(request):
    user_ids = request.data.get("user_ids", [])
    # ✅ NEW: optional group name
    group_name = (request.data.get("name") or "").strip()

    if not isinstance(user_ids, list) or not user_ids:
        return Response({"error": "user_ids must be a non-empty list"}, status=400)

    # Remove duplicates & self
    user_ids = list(set(uid for uid in user_ids if uid != request.user.id))
    if not user_ids:
        return Response({"error": "No valid users to add"}, status=400)

    users = User.objects.filter(id__in=user_ids)
    if users.count() != len(user_ids):
        return Response({"error": "One or more users not found"}, status=404)

    # 🚫 BLOCK CHECK (REQUESTER ↔ EACH USER)
    for other_user in users:
        if BlockedUser.objects.between(request.user, other_user).exists():
            return Response({"error": "Not allowed"}, status=403)

    # 🔁 PREVENT DUPLICATE GROUP (same participants, same size)
    participant_ids = sorted([request.user.id] + user_ids)

    existing_convo = (
        Conversation.objects
        .filter(participants=request.user)
        .annotate(num=models.Count("participants"))
        .filter(num=len(participant_ids))
    )
    for convo in existing_convo:
        ids = sorted(convo.participants.values_list("id", flat=True))
        if ids == participant_ids:
            return Response({"conversation_id": convo.id})

    # ➕ CREATE GROUP CONVERSATION
    convo = Conversation.objects.create(name=group_name)
    convo.participants.add(request.user, *users)

    return Response({"conversation_id": convo.id}, status=201)



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_conversations(request):
    user = request.user

    # --------------------------------------------------
    # PAGINATION
    # The endpoint used to return every conversation the user had ever
    # been part of in a single response. A power user with hundreds of
    # threads would re-pull the entire inbox on every tap of Messages.
    # Same offset/limit + limit+1 pattern used in reels_feed,
    # list_notifications, and get_page_detail.
    # --------------------------------------------------
    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 50))
    offset = max(0, offset)

    # 🚫 BLOCKED USERS (both directions)
    blocked_pairs = BlockedUser.objects.involving(user).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_user_ids = set()
    for u, b in blocked_pairs:
        blocked_user_ids.add(u)
        blocked_user_ids.add(b)
    blocked_user_ids.discard(user.id)

    # 🙈 HIDDEN CONVERSATIONS (SOFT DELETE)
    hidden_convo_ids = list(ConversationHidden.objects.filter(
        user=user
    ).values_list("conversation_id", flat=True))

    # Build the queryset with all filters applied at the DB layer so the
    # slice we apply below corresponds to actual page boundaries. The old
    # version filtered blocked-user threads in Python AFTER materialising
    # every conversation -- that prevented pagination because the page
    # size after the Python filter no longer matched what the DB returned.
    #
    # `.distinct()` is necessary because the M2M JOIN through participants
    # can otherwise produce duplicate Conversation rows when there are
    # multiple non-blocked participants. The `-id` tiebreaker keeps the
    # ordering stable across paginated requests (two conversations that
    # share an `updated_at` won't swap pages mid-pagination).
    #
    # NOTE: deliberately NOT prefetching every Message in every conversation
    # -- for a user with long chats that would load the entire message
    # corpus per inbox render. Instead we issue two bounded follow-up
    # queries below: one for the latest message of each convo, one for
    # the unread aggregate.
    base_qs = (
        user.conversations
        .exclude(id__in=hidden_convo_ids)
        .exclude(participants__id__in=blocked_user_ids)
        .distinct()
        .prefetch_related(
            "participants",
            "participants__userprofile",
        )
        .order_by("-updated_at", "-id")
    )

    # limit+1 trick -- fetch one extra row to detect has_more without a
    # separate COUNT(*) on the user's inbox.
    fetched = list(base_qs[offset:offset + limit + 1])
    has_more = len(fetched) > limit
    filtered = fetched[:limit]

    convo_ids = [c.id for c in filtered]

    # ── Latest message per conversation (one query, DB-agnostic) ────────
    last_msg_map = {}
    legacy_media_map = {}
    if convo_ids:
        latest_ids = list(
            Message.objects
            .filter(conversation_id__in=convo_ids)
            .values('conversation_id')
            .annotate(max_id=models.Max('id'))
            .values_list('max_id', flat=True)
        )
        if latest_ids:
            for m in (
                Message.objects
                .filter(id__in=latest_ids)
                .select_related("sender__userprofile")
            ):
                last_msg_map[m.conversation_id] = m

            # If the latest message stored its media only in MessageMedia
            # (no legacy `media_type` on Message), grab the first item's
            # media_type so we can render the preview emoji.
            blank_ids = [
                m.id for m in last_msg_map.values()
                if not m.is_deleted and not m.text and not m.media_type
            ]
            if blank_ids:
                for mm in (
                    MessageMedia.objects
                    .filter(message_id__in=blank_ids)
                    .order_by("message_id", "order")
                ):
                    legacy_media_map.setdefault(mm.message_id, mm.media_type)

    # ── Unread count per conversation (one aggregate query) ─────────────
    unread_map = {}
    if convo_ids:
        unread_rows = (
            Message.objects
            .filter(conversation_id__in=convo_ids, is_deleted=False)
            .exclude(sender_id=user.id)
            .exclude(read_by=user)
            .values('conversation_id')
            .annotate(unread=Count('id'))
        )
        unread_map = {r['conversation_id']: r['unread'] for r in unread_rows}

    context = {
        'request':          request,
        'viewer':           user,
        'last_msg_map':     last_msg_map,
        'legacy_media_map': legacy_media_map,
        'unread_map':       unread_map,
    }

    data = [
        ConversationSerializer(convo, context=context).data
        for convo in filtered
    ]
    return Response({
        "results": data,
        "has_more": has_more,
        "next_offset": offset + len(filtered),
    })



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delete_conversation(request):
    conversation_id = request.data.get("conversation_id")
    if not conversation_id:
        return Response({"error": "conversation_id required"}, status=400)

    convo = get_object_or_404(Conversation, id=conversation_id)
    if request.user not in convo.participants.all():
        return Response({"error": "Not allowed"}, status=403)

    ConversationHidden.objects.get_or_create(user=request.user, conversation=convo)
    return Response({"status": "hidden"})



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def rename_conversation(request):
    """
    Rename a *group* conversation (any participant can rename).
    POST /auth/conversations/rename/  { conversation_id, name }
    Broadcasts conversation.renamed to the WS group.

    Constraints:
      • Only group conversations (≥3 participants) can be renamed.
      • Name capped at ``CONVERSATION_NAME_MAX_LEN`` characters to match
        the underlying model field (the model would raise DataError on
        oversized input).
    """
    conversation_id = request.data.get("conversation_id")
    new_name        = (request.data.get("name") or "").strip()

    if not conversation_id:
        return Response({"error": "conversation_id required."}, status=400)

    if len(new_name) > CONVERSATION_NAME_MAX_LEN:
        return Response(
            {"error": f"Name too long (max {CONVERSATION_NAME_MAX_LEN} chars)."},
            status=400,
        )

    convo = get_object_or_404(
        Conversation.objects.prefetch_related("participants"),
        id=conversation_id,
    )

    if request.user not in convo.participants.all():
        return Response({"error": "Not allowed."}, status=403)

    if convo.participants.count() < 3:
        return Response(
            {"error": "Only group conversations can be renamed."},
            status=400,
        )

    convo.name = new_name
    convo.save(update_fields=["name"])

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"chat_{convo.id}",
        {
            "type": "broadcast",
            "payload": {
                "type":            "conversation.renamed",
                "conversation_id": convo.id,
                "name":            new_name,
                "renamed_by":      request.user.id,
                "username":        request.user.username,
            },
        },
    )

    return Response({"status": "renamed", "name": new_name})



@api_view(["GET"])
@permission_classes([IsAuthenticated])
def search_message_users(request):
    user = request.user
    q = request.query_params.get("q", "").strip()

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

    # 🚫 BLOCKED USERS (both directions)
    blocked_pairs = BlockedUser.objects.involving(user).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_ids = set()
    for u, b in blocked_pairs:
        blocked_ids.add(u)
        blocked_ids.add(b)
    blocked_ids.discard(user.id)

    # 💬 USERS WITH EXISTING CONVERSATIONS
    convo_user_ids = set(
        Conversation.objects
        .filter(participants=user)
        .values_list("participants", flat=True)
    )
    convo_user_ids.discard(user.id)

    # 👥 USERS I FOLLOW
    following_ids = set(
        Follow.objects.filter(follower=user).values_list("following_id", flat=True)
    )

    # 🔍 SEARCH USERS
    #
    # Rank in the DB instead of pulling every username match into memory to
    # sort + slice in Python: existing conversation (0) ranks above followed
    # (1) above everyone else (2), then alphabetical, with id as the final
    # tiebreaker so the offset window stays stable across pages.
    users = (
        User.objects
        .filter(username__icontains=q)
        .exclude(id=user.id)
        .exclude(id__in=blocked_ids)
        .select_related("userprofile")
        .annotate(
            convo_rank=Case(
                When(id__in=convo_user_ids, then=Value(0)),
                When(id__in=following_ids, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("convo_rank", "username", "id")
    )

    # Offset window: fetch one extra row to detect `has_more` without a COUNT.
    window = list(users[offset : offset + limit + 1])
    has_more = len(window) > limit
    window = window[:limit]

    results = []
    for u in window:
        up = getattr(u, "userprofile", None)

        results.append({
            "id": u.id,
            "username": u.username,
            "avatar": (
                request.build_absolute_uri(up.avatar.url)
                if up and up.avatar
                else None
            ),
            "rank": u.convo_rank,
        })

    return Response({
        "results": results,
        "has_more": has_more,
        "next_offset": (offset + limit) if has_more else None,
    })
