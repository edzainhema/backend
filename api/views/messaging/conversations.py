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

from ...models import (
    BlockedUser, Conversation, ConversationHidden,
    ConversationParticipantState, Follow, Message, MessageMedia,
)
from ...serializers import ConversationSerializer
from ...services.conversations import (
    _count_request_creation,
    accepted_state_exists_for,
    check_request_throttle,
    compute_initial_acceptance,
    ensure_participant_states,
    mark_accepted,
)

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

    if BlockedUser.objects.between(request.user, other_user).exists():
        return Response({"error": "Not allowed"}, status=403)

    # M7: if `other_user` doesn't follow `request.user`, the conversation
    # will be created in their REQUESTS inbox -- count it against the
    # per-sender request throttle BEFORE we create anything. Following
    # the existing convention this is keyed in the cache, not in DB,
    # so the check is essentially free.
    will_be_request = not Follow.objects.filter(
        follower=other_user, following=request.user,
    ).exists()
    if will_be_request:
        # Skip the throttle if a 1:1 conversation already exists with
        # this user -- "starting" an existing thread isn't a NEW
        # request. Uses the two-query candidate_ids pattern (same as
        # the find-or-create dance below): chained M2M
        # `.filter(participants=...).filter(participants=...)` plus
        # `.annotate(Count("participants"))` produces inflated counts
        # because Django joins the through-table for each filter AND
        # the aggregate, double-counting rows.
        candidate_ids = list(
            Conversation.objects
            .filter(participants=request.user)
            .filter(participants=other_user)
            .values_list("id", flat=True)
        )
        existing = (
            Conversation.objects
            .filter(id__in=candidate_ids)
            .annotate(num=models.Count("participants"))
            .filter(num=2)
            .exists()
        )
        if not existing:
            err = check_request_throttle(request.user.id)
            if err:
                return Response({"error": err}, status=429)

    created = False
    with transaction.atomic():
        list(
            User.objects
            .select_for_update()
            .filter(id__in=sorted({request.user.id, other_user.id}))
            .order_by('id')
        )

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
            ensure_participant_states(
                convo.id, request.user.id,
                [request.user.id, other_user.id],
            )
            created = True

        ConversationHidden.objects.filter(
            user=request.user, conversation=convo
        ).delete()

    # M7: count the request creation against the rolling cap ONLY if we
    # actually created a new conversation AND it's a request from the
    # recipient's perspective. Re-opening an existing thread doesn't
    # consume the quota.
    if created and will_be_request:
        _count_request_creation(request.user.id)

    return Response({"conversation_id": convo.id})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def start_group_conversation(request):
    user_ids = request.data.get("user_ids", [])
    group_name = (request.data.get("name") or "").strip()

    if not isinstance(user_ids, list) or not user_ids:
        return Response({"error": "user_ids must be a non-empty list"}, status=400)

    user_ids = list(set(uid for uid in user_ids if uid != request.user.id))
    if not user_ids:
        return Response({"error": "No valid users to add"}, status=400)

    users = User.objects.filter(id__in=user_ids)
    if users.count() != len(user_ids):
        return Response({"error": "One or more users not found"}, status=404)

    for other_user in users:
        if BlockedUser.objects.between(request.user, other_user).exists():
            return Response({"error": "Not allowed"}, status=403)

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

    # M7: pre-flight the request throttle if ANY recipient doesn't
    # follow the creator. A group with even one non-follower is a
    # request for that recipient and counts.
    initial_acceptance = compute_initial_acceptance(
        request.user.id, user_ids,
    )
    has_request_recipient = any(
        not accepted for accepted in initial_acceptance.values()
    )
    if has_request_recipient:
        err = check_request_throttle(request.user.id)
        if err:
            return Response({"error": err}, status=429)

    convo = Conversation.objects.create(name=group_name)
    convo.participants.add(request.user, *users)
    ensure_participant_states(
        convo.id, request.user.id,
        [request.user.id] + list(user_ids),
    )

    if has_request_recipient:
        _count_request_creation(request.user.id)

    return Response({"conversation_id": convo.id}, status=201)


def _list_conversations_impl(request, *, accepted: bool):
    """Shared body for the main-inbox listing (``accepted=True``) and the
    requests-inbox listing (``accepted=False``). The only difference
    between the two is the participant-state filter. M7.
    """
    user = request.user

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

    blocked_pairs = BlockedUser.objects.involving(user).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_user_ids = set()
    for u, b in blocked_pairs:
        blocked_user_ids.add(u)
        blocked_user_ids.add(b)
    blocked_user_ids.discard(user.id)

    hidden_convo_ids = list(ConversationHidden.objects.filter(
        user=user
    ).values_list("conversation_id", flat=True))

    # M7: pick the right state bucket for THIS caller. Conversations
    # where a state row is missing (legacy / pre-migration) are
    # treated as accepted so they land in the main inbox -- same
    # grandfather rule as `is_accepted_for` in the service.
    if accepted:
        state_convo_ids = list(
            ConversationParticipantState.objects
            .filter(user=user, is_accepted=True)
            .values_list("conversation_id", flat=True)
        )
        # Also include any of the user's conversations with NO state
        # row at all (defensive fallback for grandfathered data the
        # migration somehow didn't reach).
        with_state_ids = set(
            ConversationParticipantState.objects
            .filter(user=user)
            .values_list("conversation_id", flat=True)
        )
        ungranted_ids = list(
            user.conversations.exclude(id__in=with_state_ids)
            .values_list("id", flat=True)
        )
        include_ids = set(state_convo_ids) | set(ungranted_ids)
        base_qs = Conversation.objects.filter(id__in=include_ids)
    else:
        state_convo_ids = list(
            ConversationParticipantState.objects
            .filter(user=user, is_accepted=False)
            .values_list("conversation_id", flat=True)
        )
        base_qs = Conversation.objects.filter(id__in=state_convo_ids)

    base_qs = (
        base_qs
        .exclude(id__in=hidden_convo_ids)
        .exclude(participants__id__in=blocked_user_ids)
        .distinct()
        .prefetch_related(
            "participants",
            "participants__userprofile",
        )
        .order_by("-updated_at", "-id")
    )

    fetched = list(base_qs[offset:offset + limit + 1])
    has_more = len(fetched) > limit
    filtered = fetched[:limit]

    convo_ids = [c.id for c in filtered]

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
                .select_related(
                    "sender__userprofile",
                    # Pre-join the shared post + its author so the inbox preview
                    # "<user> sent a post by <author>" can format from cache,
                    # not a per-row follow-up query.
                    "shared_post",
                    "shared_post__user",
                )
            ):
                last_msg_map[m.conversation_id] = m

            # If the latest message stored its media only in MessageMedia
            # (no legacy `media_type` on Message), grab the first item's
            # media_type so we can render the preview emoji. Skip shared-post
            # messages -- their preview comes from shared_post, not media.
            blank_ids = [
                m.id for m in last_msg_map.values()
                if not m.is_deleted
                and not m.text
                and not m.media_type
                and not m.shared_post_id
            ]
            if blank_ids:
                for mm in (
                    MessageMedia.objects
                    .filter(message_id__in=blank_ids)
                    .order_by("message_id", "order")
                ):
                    legacy_media_map.setdefault(mm.message_id, mm.media_type)

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


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_conversations(request):
    """Main inbox: conversations the caller has accepted (or which were
    grandfathered as accepted by migration 0100)."""
    return _list_conversations_impl(request, accepted=True)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_message_requests(request):
    """Requests inbox: conversations from senders the caller doesn't
    follow that haven't been accepted yet (M7). Identical response
    shape to ``list_conversations`` so the frontend can render with
    the same components -- only the filter differs."""
    return _list_conversations_impl(request, accepted=False)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def accept_message_request(request):
    """Explicit accept: flips the caller's state row to is_accepted=True
    so the conversation moves from their requests inbox to their main
    inbox. Idempotent -- accepting an already-accepted conversation
    returns 200 with status=already_accepted.

    The implicit-accept path (sending a reply) calls the same
    `mark_accepted` helper, so a caller who replies without
    explicitly accepting first ends up in the same state.
    """
    conversation_id = request.data.get("conversation_id")
    if not conversation_id:
        return Response({"error": "conversation_id required"}, status=400)

    try:
        convo = Conversation.objects.get(id=conversation_id)
    except Conversation.DoesNotExist:
        return Response({"error": "Conversation not found"}, status=404)

    if request.user not in convo.participants.all():
        return Response({"error": "Not allowed"}, status=403)

    flipped = mark_accepted(convo.id, request.user.id)
    return Response({
        "status": "accepted" if flipped else "already_accepted",
        "conversation_id": convo.id,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def decline_message_request(request):
    """Decline: hide the conversation from the caller (via the existing
    ConversationHidden mechanism, identical to delete_conversation).

    The sender is silently uninformed -- the Instagram pattern. No
    "rejected" notification, no read receipt. The conversation row
    itself is preserved so the OTHER participant(s) keep their copy
    of the thread; only the caller stops seeing it.

    A future message from the same sender to the same caller will
    re-create the state and put the conversation back in requests
    -- decline is per-thread, not a permanent block. Use the block
    endpoint for permanent silencing.
    """
    conversation_id = request.data.get("conversation_id")
    if not conversation_id:
        return Response({"error": "conversation_id required"}, status=400)

    try:
        convo = Conversation.objects.get(id=conversation_id)
    except Conversation.DoesNotExist:
        return Response({"error": "Conversation not found"}, status=404)

    if request.user not in convo.participants.all():
        return Response({"error": "Not allowed"}, status=403)

    ConversationHidden.objects.get_or_create(
        user=request.user, conversation=convo,
    )
    return Response({"status": "declined", "conversation_id": convo.id})


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
    """Rename a group conversation (>=3 participants)."""
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

    blocked_pairs = BlockedUser.objects.involving(user).values_list(
        "user_id", "blocked_user_id"
    )

    blocked_ids = set()
    for u, b in blocked_pairs:
        blocked_ids.add(u)
        blocked_ids.add(b)
    blocked_ids.discard(user.id)

    convo_user_ids = set(
        Conversation.objects
        .filter(participants=user)
        .values_list("participants", flat=True)
    )
    convo_user_ids.discard(user.id)

    following_ids = set(
        Follow.objects.filter(follower=user).values_list("following_id", flat=True)
    )

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
