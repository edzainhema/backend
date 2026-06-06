"""M7: message-requests model + acceptance helpers.

The product rule (Instagram-style): a conversation lands in a participant's
*main inbox* iff that participant has accepted it. Accepting happens
either explicitly (tap "Accept") or implicitly (send a message in
the conversation). Until then, the conversation lives in their
*requests* inbox.

The initial acceptance for each participant is decided at conversation
creation, based on whether that participant FOLLOWS the originator.
The follow direction here is "recipient follows sender = trusted":
following someone is consent to receive their DMs. The originator's
own state is always accepted (they initiated).

Why a separate state model instead of a flag on Conversation:
groups need per-participant bucketing. If A creates a group with B
and C and only B follows A, B sees the group in their main inbox
while C sees it in requests. A flat conversation-level flag can't
express that.

Functions in this module are sync and DB-touching. The WS consumer
wraps them in `database_sync_to_async`; views call them directly.
"""
import time

from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from ..models import (
    Conversation,
    ConversationParticipantState,
    Follow,
)


# --------------------------------------------------------------------------
# Per-recipient request-creation throttle
# --------------------------------------------------------------------------
# Caps how many NEW *request* conversations a single sender can open per
# rolling window. Once a recipient has accepted (or the recipient
# follows the sender so it never was a request), subsequent message
# sends fall under the regular SendMessageRateThrottle and don't
# count here. This is the "per-sender throttle on starts to non-
# followers" lever from the M7 design notes.
REQUEST_HOURLY_CAP = 5
REQUEST_DAILY_CAP = 20


def _request_hour_key(sender_id: int) -> str:
    return f"msg_request_hour:{sender_id}:{int(time.time() // 3600)}"


def _request_day_key(sender_id: int) -> str:
    return f"msg_request_day:{sender_id}:{int(time.time() // 86400)}"


def _count_request_creation(sender_id: int) -> None:
    """Bump the rolling counters. Caller has already confirmed via
    `check_request_throttle` that the cap isn't tripped."""
    hk = _request_hour_key(sender_id)
    dk = _request_day_key(sender_id)
    cache.set(hk, (cache.get(hk) or 0) + 1, 3600)
    cache.set(dk, (cache.get(dk) or 0) + 1, 86400)


def check_request_throttle(sender_id: int) -> str | None:
    """Pre-flight check for opening a new request-state conversation.

    Returns None if the caller is under both caps and the conversation
    can be created. Returns an error message string if they're tripped,
    so the view can map it to a 429 with that body.

    Doesn't bump the counter -- call `_count_request_creation` after
    you've confirmed the conversation actually went to is_accepted=False
    for at least one recipient (i.e., it really is a request).
    """
    hourly = cache.get(_request_hour_key(sender_id)) or 0
    daily = cache.get(_request_day_key(sender_id)) or 0
    if hourly >= REQUEST_HOURLY_CAP:
        return (
            f"Too many message requests in the last hour "
            f"(max {REQUEST_HOURLY_CAP}). Try again later."
        )
    if daily >= REQUEST_DAILY_CAP:
        return (
            f"Daily message request limit reached "
            f"(max {REQUEST_DAILY_CAP}). Try again tomorrow."
        )
    return None


# --------------------------------------------------------------------------
# Per-recipient initial acceptance
# --------------------------------------------------------------------------

def compute_initial_acceptance(
    creator_id: int, recipient_ids: list[int],
) -> dict[int, bool]:
    """Return {recipient_id: is_accepted} for a brand-new conversation.

    Rule: a recipient who FOLLOWS the creator pre-trusted the
    creator's messages, so their state starts is_accepted=True
    (direct inbox). Anyone else starts is_accepted=False (request).

    One DB query regardless of recipient count.
    """
    if not recipient_ids:
        return {}
    following_ids = set(
        Follow.objects.filter(
            follower_id__in=recipient_ids,
            following_id=creator_id,
        ).values_list("follower_id", flat=True)
    )
    return {rid: (rid in following_ids) for rid in recipient_ids}


def ensure_participant_states(
    conversation_id: int,
    creator_id: int,
    participant_ids: list[int],
) -> dict[int, bool]:
    """Create the ConversationParticipantState rows for a freshly-
    created conversation. The creator's state is always accepted;
    each non-creator recipient is accepted iff they follow the creator.

    Returns the {user_id: is_accepted} map (useful for the caller's
    push-gating + response shape decisions).

    Idempotent via the (user, conversation) unique constraint: if the
    conversation already had states (e.g., a re-entry into
    start_conversation that found an existing convo), this is a no-op
    for those rows -- but the caller almost always avoids that by
    branching on "convo just created" vs "convo found."
    """
    non_creators = [pid for pid in participant_ids if pid != creator_id]
    initial = compute_initial_acceptance(creator_id, non_creators)
    initial[creator_id] = True  # originator always accepts

    now = timezone.now()
    rows = []
    for user_id, is_accepted in initial.items():
        rows.append(ConversationParticipantState(
            user_id=user_id,
            conversation_id=conversation_id,
            is_accepted=is_accepted,
            accepted_at=now if is_accepted else None,
        ))
    ConversationParticipantState.objects.bulk_create(
        rows, ignore_conflicts=True,
    )
    return initial


# --------------------------------------------------------------------------
# Read helpers (used by views + push gating)
# --------------------------------------------------------------------------

def is_accepted_for(conversation_id: int, user_id: int) -> bool:
    """Return True if the user has the conversation in their main
    inbox. Treats a missing state row as ACCEPTED -- so an
    unmigrated / legacy row never strands a conversation in
    requests by accident. (Grandfathering safety net; backfill
    migration already creates these.)
    """
    try:
        return ConversationParticipantState.objects.only("is_accepted").get(
            conversation_id=conversation_id, user_id=user_id,
        ).is_accepted
    except ConversationParticipantState.DoesNotExist:
        return True


def get_accepted_recipient_ids(
    conversation_id: int, sender_id: int,
) -> set[int]:
    """Return the set of recipient ids whose state is is_accepted=True.

    Used by push fan-out to skip recipients who haven't accepted yet
    (request inbox = silent; the M7 design call). Missing state rows
    are treated as accepted, same as `is_accepted_for`.
    """
    rows = ConversationParticipantState.objects.filter(
        conversation_id=conversation_id,
    ).exclude(user_id=sender_id).values_list("user_id", "is_accepted")
    accepted = set()
    seen = set()
    for uid, accepted_flag in rows:
        seen.add(uid)
        if accepted_flag:
            accepted.add(uid)
    # Any participant without a state row defaults to accepted (grandfather).
    convo_participant_ids = set(
        Conversation.objects.get(id=conversation_id)
        .participants.exclude(id=sender_id)
        .values_list("id", flat=True)
    )
    accepted |= (convo_participant_ids - seen)
    return accepted


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------

def mark_accepted(conversation_id: int, user_id: int) -> bool:
    """Flip the user's state for this conversation to is_accepted=True.

    Returns True if the row was flipped (was False, now True), False
    if it was already True (no-op) or no state row exists. Idempotent.

    Called by:
      * the explicit accept endpoint (`accept_message_request`)
      * the implicit-accept-on-reply path in `send_message` and the
        WS consumer's `create_chat_message` service
    """
    now = timezone.now()
    updated = ConversationParticipantState.objects.filter(
        conversation_id=conversation_id,
        user_id=user_id,
        is_accepted=False,
    ).update(is_accepted=True, accepted_at=now)
    return updated > 0


def accepted_state_exists_for(
    conversation_id: int, user_id: int,
) -> bool:
    """Tiny helper -- True if a state row exists at all (regardless
    of value). Used by the implicit-accept fast path to avoid a save
    when no row was ever created (defensive)."""
    return ConversationParticipantState.objects.filter(
        conversation_id=conversation_id, user_id=user_id,
    ).exists()
