"""Share a post into one or more DMs, and list the contacts to share with.

Both endpoints back the in-app "Share post" modal:

  • list_share_recipients — returns the ranked list of friends to populate
    the modal. Prefers the precomputed close-friends set (UserCloseFriends),
    falls back to recent DM partners, then followers — every layer dedup'd
    and block-filtered. One round-trip; the modal shows the result with no
    extra paging on first open.

  • share_post_to_users — accepts a post id + a list of recipient user ids,
    creates / reuses the 1:1 DM with each one, and writes a Message row with
    ``shared_post`` set so the bubble renders the shared-post preview card.
    Broadcasts each message over the conversation WS group and fires the
    standard new-message push. Per-recipient errors are isolated (a block
    against one user doesn't abort the others).
"""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.models import User
from django.db import models as dj_models
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import (
    BlockedUser,
    Conversation,
    ConversationHidden,
    Follow,
    Message,
    Post,
    UserCloseFriends,
)
from ...serializers import MessageSerializer
from ...services.conversations import (
    _count_request_creation,
    check_request_throttle,
    ensure_participant_states,
    is_accepted_for,
    mark_accepted,
)
from ...services.feed_helpers import viewer_can_see_post
from ...services.push import push_to_user
from ...services.throttles import SendMessageRateThrottle


MAX_RECIPIENTS_PER_SHARE = 20  # Hard cap so a runaway client can't fan out
                               # an unbounded blast of DMs in one call.
RECIPIENT_LIST_LIMIT = 50      # Max friends returned by list_share_recipients.


def _avatar_url(request, user) -> str | None:
    p = getattr(user, 'userprofile', None)
    if p and p.avatar:
        return request.build_absolute_uri(p.avatar.url)
    return None


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_share_recipients(request):
    """
    Friends to populate the Share modal. Ranked, dedup'd, block-filtered.

    Source priority (highest first):
      1. UserCloseFriends.friend_ids — the precomputed top-N close-friends set
         (UB-1). These are the people the user interacts with most.
      2. Recent DM partners — anyone they've exchanged a Message with in their
         most recent conversations.
      3. Mutual / one-way follows — gives a sensible non-empty list to brand-
         new users who don't have an interaction history yet.

    Response: { "results": [{id, username, avatar, is_close_friend}], "has_more": false }
    """
    user = request.user

    blocked_pairs = BlockedUser.objects.involving(user).values_list(
        "user_id", "blocked_user_id"
    )
    blocked_ids = set()
    for u, b in blocked_pairs:
        blocked_ids.add(u)
        blocked_ids.add(b)
    blocked_ids.discard(user.id)

    ranked_ids: list[int] = []
    seen: set[int] = set()

    def _push(uid: int) -> None:
        if uid in seen or uid == user.id or uid in blocked_ids:
            return
        seen.add(uid)
        ranked_ids.append(uid)

    # 1. Close friends (precomputed). Order preserved from friend_ids.
    try:
        cf = UserCloseFriends.objects.only("friend_ids").get(user=user)
        close_friend_ids = set(cf.friend_ids or [])
        for uid in cf.friend_ids or []:
            _push(int(uid))
    except UserCloseFriends.DoesNotExist:
        close_friend_ids = set()

    # 2. Recent DM partners. One query: most-recently-updated conversations,
    # collect the other participant id(s).
    recent_convos = (
        Conversation.objects
        .filter(participants=user)
        .order_by("-updated_at")
        .prefetch_related("participants")[:30]
    )
    for c in recent_convos:
        for p in c.participants.all():
            if p.id != user.id:
                _push(p.id)
                if len(ranked_ids) >= RECIPIENT_LIST_LIMIT:
                    break
        if len(ranked_ids) >= RECIPIENT_LIST_LIMIT:
            break

    # 3. Top-up with follows so the list is never empty for new users.
    if len(ranked_ids) < RECIPIENT_LIST_LIMIT:
        follow_ids = list(
            Follow.objects.filter(follower=user)
            .order_by("-created_at")
            .values_list("following_id", flat=True)[:RECIPIENT_LIST_LIMIT]
        )
        for uid in follow_ids:
            _push(uid)
            if len(ranked_ids) >= RECIPIENT_LIST_LIMIT:
                break

    ranked_ids = ranked_ids[:RECIPIENT_LIST_LIMIT]
    if not ranked_ids:
        return Response({"results": [], "has_more": False})

    # Hydrate. Preserve the ranked order by sorting the fetched users back
    # into the order of ranked_ids (a small in-Python sort beats issuing
    # N point queries).
    users = list(
        User.objects.filter(id__in=ranked_ids).select_related("userprofile")
    )
    by_id = {u.id: u for u in users}
    results = []
    for uid in ranked_ids:
        u = by_id.get(uid)
        if u is None:
            continue
        results.append({
            "id": u.id,
            "username": u.username,
            "avatar": _avatar_url(request, u),
            "is_close_friend": uid in close_friend_ids,
        })

    return Response({"results": results, "has_more": False})


def _resolve_or_create_dm(viewer, other):
    """Find or create the 1:1 DM between viewer and other. Mirrors
    start_conversation's two-query find-or-create dance so concurrent shares
    don't produce duplicate conversation rows.

    M7: on fresh creation, also seed ConversationParticipantState rows
    so the resulting message lands in the right inbox bucket (main vs
    requests) for `other`. The viewer is always accepted; `other`
    accepts iff they follow the viewer."""
    candidate_ids = list(
        Conversation.objects
        .filter(participants=viewer)
        .filter(participants=other)
        .values_list("id", flat=True)
    )
    convo = (
        Conversation.objects
        .filter(id__in=candidate_ids)
        .annotate(num_participants=dj_models.Count("participants"))
        .filter(num_participants=2)
        .first()
    )
    if convo is None:
        convo = Conversation.objects.create()
        convo.participants.add(viewer, other)
        ensure_participant_states(
            convo.id, viewer.id, [viewer.id, other.id],
        )
    return convo


def _broadcast_shared(convo, message_data) -> None:
    """Push the new shared-post message onto the conversation's WS group so
    any open DM screen renders it without a round-trip back to /messages/."""
    payload = {
        **message_data,
        # Receivers compute is_mine off sender_id; force False on the
        # broadcast and let the sender's local optimistic insert keep its
        # own is_mine=True.
        "is_mine": False,
    }
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"chat_{convo.id}",
        {
            "type": "chat.media_message",
            "payload": {"type": "message.new", "message": payload},
        },
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([SendMessageRateThrottle])
def share_post_to_users(request):
    """
    Forward a post into N individual DMs.

    POST /auth/messages/share-post/
      { post_id: int, user_ids: [int, ...], text?: str }

    Each recipient gets their own DM conversation (find-or-created) with a
    Message row whose ``shared_post`` FK points at the post — the bubble
    renders a tappable post-preview card on receipt. Per-recipient failures
    (blocked, deleted account) are reported individually so the modal can
    show which ones went through.
    """
    post_id = request.data.get('post_id')
    user_ids = request.data.get('user_ids') or []
    text = (request.data.get('text') or '').strip()

    if not post_id:
        return Response({"error": "post_id is required."}, status=400)
    if not isinstance(user_ids, list) or not user_ids:
        return Response({"error": "user_ids must be a non-empty list."}, status=400)

    # Dedupe + drop self, then cap.
    try:
        unique_ids = []
        seen = set()
        for raw in user_ids:
            uid = int(raw)
            if uid == request.user.id or uid in seen:
                continue
            seen.add(uid)
            unique_ids.append(uid)
    except (TypeError, ValueError):
        return Response({"error": "user_ids must contain integers."}, status=400)

    if not unique_ids:
        return Response({"error": "No valid recipients."}, status=400)
    if len(unique_ids) > MAX_RECIPIENTS_PER_SHARE:
        return Response(
            {"error": f"Too many recipients (max {MAX_RECIPIENTS_PER_SHARE})."},
            status=400,
        )

    try:
        post = (
            Post.objects
            .select_related("user", "user__userprofile", "page")
            .get(id=post_id)
        )
    except Post.DoesNotExist:
        return Response({"error": "Post not found."}, status=404)

    # H1: the share endpoint must NOT let a viewer forward a post they
    # can't see. Without this check, a user who guesses or enumerates a
    # post id from a private page / private-account profile could share
    # the post (and its preview card -- author, thumbnail, description)
    # into any DM. Mirrors the gate `toggle_post_like` / `get_comments`
    # use. Return the SAME 404 message we return for a genuinely missing
    # post so the endpoint can't be turned into a "does this id exist"
    # probe for hidden content.
    #
    # `viewer_can_see_post` reads `post.user`, `post.user.userprofile`,
    # and `post.page`; those are pre-joined by `select_related` above so
    # the gate adds zero extra queries on the hot path. Sharing your own
    # post is always allowed because `viewer_can_see_post` short-circuits
    # `True` when `post.user_id == viewer.id`.
    if not viewer_can_see_post(request.user, post):
        return Response({"error": "Post not found."}, status=404)

    recipients = list(
        User.objects.filter(id__in=unique_ids).select_related("userprofile")
    )
    found_ids = {u.id for u in recipients}
    missing = [uid for uid in unique_ids if uid not in found_ids]

    sent = []
    failures = [{"user_id": uid, "error": "User not found."} for uid in missing]

    # M7: bulk-load which recipients follow the sender. Used below to
    # decide whether each share would CREATE a new request-state
    # conversation (and therefore consume the per-sender request
    # throttle quota). One query covers all recipients instead of N.
    recipient_user_ids = [u.id for u in recipients]
    follower_back_ids = set(
        Follow.objects.filter(
            follower_id__in=recipient_user_ids,
            following=request.user,
        ).values_list("follower_id", flat=True)
    )

    for other in recipients:
        # Block check, both directions. Same rule the REST send_message and
        # the WS consumer enforce; mirroring it here means a blocked user
        # can't be DM'd a share even when they appear in the list.
        if BlockedUser.objects.between(request.user, other).exists():
            failures.append({"user_id": other.id, "error": "Not allowed."})
            continue

        # M7: per-recipient request throttle. Fires only when this share
        # would create a NEW request-state conversation -- i.e., the
        # recipient doesn't follow the sender AND no 1:1 convo already
        # exists between them. Sharing to a follower, or sharing into
        # an existing thread (regardless of acceptance), does not
        # consume the request quota -- same rule as start_conversation.
        #
        # The throttle is checked + counted per-recipient so a 20-target
        # share where the 6th non-follower trips the cap still delivers
        # the first 5 successes; the rest get a per-recipient failure
        # entry. Mirrors the per-recipient block-failure pattern this
        # endpoint already uses.
        will_consume_request_quota = False
        if other.id not in follower_back_ids:
            candidate_ids = list(
                Conversation.objects
                .filter(participants=request.user)
                .filter(participants=other)
                .values_list("id", flat=True)
            )
            existing = (
                Conversation.objects
                .filter(id__in=candidate_ids)
                .annotate(num=dj_models.Count("participants"))
                .filter(num=2)
                .exists()
            )
            if not existing:
                err = check_request_throttle(request.user.id)
                if err:
                    failures.append({"user_id": other.id, "error": err})
                    continue
                will_consume_request_quota = True

        try:
            with transaction.atomic():
                convo = _resolve_or_create_dm(request.user, other)

                # If either side had soft-hidden this convo, un-hide so a
                # share re-surfaces it in the inbox like a fresh DM does.
                ConversationHidden.objects.filter(
                    conversation=convo,
                    user__in=convo.participants.all(),
                ).delete()

                message = Message.objects.create(
                    conversation=convo,
                    sender=request.user,
                    text=text,
                    shared_post=post,
                )
                Conversation.objects.filter(id=convo.id).update(
                    updated_at=timezone.now()
                )
        except Exception as exc:  # pragma: no cover - defensive
            failures.append({"user_id": other.id, "error": str(exc)})
            continue

        data = MessageSerializer(
            message,
            context={"request": request, "viewer": request.user},
        ).data

        # Live broadcast so an open DM screen renders the bubble instantly.
        _broadcast_shared(convo, data)

        # M7: count this share against the per-sender request quota
        # AFTER the message has been committed -- if the DB write
        # failed mid-loop, the convo wasn't actually created and the
        # quota shouldn't be charged.
        if will_consume_request_quota:
            _count_request_creation(request.user.id)

        # M7: if the recipient hasn't accepted this conversation yet
        # (request inbox), suppress the push -- same rule as
        # _push_new_message. A shared-post into a request thread is
        # silent on the recipient's device until they accept.
        if is_accepted_for(convo.id, other.id):
            # Push notification — mirrors send_message's per-recipient
            # fan-out so multi-account devices route correctly. Failures
            # here must not abort the share itself; recipient already
            # has the message.
            try:
                push_to_user(
                    other,
                    title=request.user.username,
                    body=text or f"Shared a post",
                    extra_data={
                        "type": "message",
                        "conversation_id": convo.id,
                        "sender_id": request.user.id,
                        "shared_post_id": post.id,
                    },
                )
            except Exception:
                pass

        sent.append({
            "user_id": other.id,
            "conversation_id": convo.id,
            "message_id": message.id,
        })

    return Response({"sent": sent, "failures": failures}, status=200 if sent else 400)
