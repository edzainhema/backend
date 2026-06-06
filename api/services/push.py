"""Server-side push notifications (FCM). firebase-admin is optional (see
backend/firebase.py): guarded so this module imports and no-ops without it."""
import logging

try:
    from firebase_admin import messaging, exceptions as firebase_exceptions
except ImportError:  # pragma: no cover - exercised only without the SDK installed
    messaging = None
    firebase_exceptions = None

logger = logging.getLogger(__name__)


def send_push_notification(tokens, title, body, data=None):
    """
    Send a push notification to one or more FCM tokens.

    `data` (optional) is a dict of string→string entries that ride along in
    the FCM `data` payload — used by the frontend to route per-account
    pushes (see push_to_user). FCM requires all data values to be strings;
    this function coerces them.

    Inspects per-token results so we can:
      - log which tokens failed and why (instead of silently swallowing)
      - prune Device rows whose tokens FCM has flagged as permanently invalid
        (UnregisteredError, SenderIdMismatchError, invalid-argument), so dead
        tokens stop polluting future sends.
    """
    if messaging is None:
        # firebase-admin not installed / not configured (see backend/firebase.py).
        # Push is unavailable in this environment; no-op instead of crashing.
        return
    if not tokens:
        return

    # Deduplicate — multiple Device rows can hold the same token if a user
    # registered from multiple installs/sessions. Sending duplicates wastes
    # the per-message FCM quota and produces noisy logs.
    tokens = list({t for t in tokens if t})
    if not tokens:
        return

    # FCM data payload values must all be strings. Quietly coerce instead of
    # making every caller stringify by hand.
    coerced_data = (
        {str(k): str(v) for k, v in data.items() if v is not None}
        if data
        else None
    )

    message = messaging.MulticastMessage(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=coerced_data,
        tokens=tokens,
    )

    try:
        response = messaging.send_each_for_multicast(message)
    except Exception as exc:
        # Network error, auth error, etc. — must never break the request path.
        logger.error(f"[push] send_each_for_multicast crashed: {exc!r}")
        return

    # Tokens that should be deleted from the DB because FCM says they're dead.
    dead_tokens = []

    for idx, resp in enumerate(response.responses):
        if resp.success:
            continue

        token = tokens[idx]
        exc = resp.exception
        logger.warning(f"[push] token={token[:16]}… failed: {exc!r}")

        # These three error classes are FCM's way of saying "this token will
        # never work again — stop sending to it." Anything else (network
        # blips, quota, server errors) should be retried on the next send,
        # so we leave those rows alone.
        if isinstance(
            exc,
            (
                messaging.UnregisteredError,
                messaging.SenderIdMismatchError,
            ),
        ) or (
            isinstance(exc, firebase_exceptions.InvalidArgumentError)
        ):
            dead_tokens.append(token)

    if dead_tokens:
        # Imported lazily to avoid a circular import at module load.
        from ..models import Device
        deleted, _ = Device.objects.filter(token__in=dead_tokens).delete()
        logger.info(f"[push] pruned {deleted} dead Device row(s)")

    logger.info(
        f"[push] sent ok={response.success_count} "
        f"failed={response.failure_count} total={len(tokens)}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Per-user push helper (multi-account aware)
# ──────────────────────────────────────────────────────────────────────────────

def _recipient_has_muted(recipient_id, actor_id=None, page_id=None) -> bool:
    """M11: return True if `recipient` has muted `actor` OR `page`.

    Reads `MutedUser` / `MutedPage` directly -- no caching, since
    a stale mute set would let unwanted pushes slip through and the
    mute set is small per user. Either id may be None (skip that
    check); if BOTH are None this returns False unconditionally.
    """
    from ..models import MutedPage, MutedUser
    if actor_id is not None and MutedUser.objects.filter(
        user_id=recipient_id, muted_user_id=actor_id,
    ).exists():
        return True
    if page_id is not None and MutedPage.objects.filter(
        user_id=recipient_id, page_id=page_id,
    ).exists():
        return True
    return False


def recipients_who_muted(recipient_ids, *, actor_id=None, page_id=None) -> set:
    """M11 (bulk): of ``recipient_ids``, return the subset that has muted
    ``actor`` (user) OR ``page`` — i.e. the recipients a push from this source
    must SKIP.

    This is the many-recipient counterpart to ``_recipient_has_muted``: ONE
    query per non-None source (not one per recipient), so a chat fan-out can
    drop muted recipients before enqueuing. It is deliberately the SAME rule as
    ``_recipient_has_muted`` (which the single-recipient REST path applies via
    ``push_to_user(actor=...)``) and lives right next to it, so the WebSocket
    fan-out and the REST path can't drift on what "muted" means (audit H2).
    Either id may be None to skip that side; both None → empty set.
    """
    from ..models import MutedPage, MutedUser
    ids = [r for r in (recipient_ids or []) if r]
    if not ids:
        return set()
    muted: set = set()
    if actor_id is not None:
        muted.update(
            MutedUser.objects.filter(
                user_id__in=ids, muted_user_id=actor_id,
            ).values_list("user_id", flat=True)
        )
    if page_id is not None:
        muted.update(
            MutedPage.objects.filter(
                user_id__in=ids, page_id=page_id,
            ).values_list("user_id", flat=True)
        )
    return muted


def push_to_user(recipient, title, body, extra_data=None, *, actor=None, page=None):
    """
    Queue a push notification to every device that has `recipient` registered,
    and return immediately (BACKEND_SCALING_AUDIT.md SY-2).

    This is the public entry point used by ALL user-targeted pushes — like,
    comment, follow, mention, DM media, page actions, etc. It USED to do the
    Device lookup and the blocking FCM `send_each_for_multicast` network call
    inline on the request's own thread, so every one of those actions paid an
    FCM round trip (and the mention / page-action paths paid several, in a
    loop) before the API could respond. FCM latency and outages translated
    directly into API latency and tied-up workers.

    Now it just enqueues the work onto the Celery queue (INF-5). The caller's
    notification ROW write stays inline (it happens at the call site, before
    this); only the network send is deferred. The actual Device query + FCM
    send live in `_send_push_to_user`, which the `dispatch_push` task invokes
    on a worker.

    Behaviour is unchanged with no broker configured: settings keep Celery in
    EAGER mode, so `.delay(...)` runs the task inline and the push still goes
    out within the request, exactly as before — just refactored. Once a broker
    + worker are running it becomes truly asynchronous.

    `extra_data` (optional) is merged into the FCM data payload — useful for
    deep-link info like {"type": "follow_request", "actor_id": 7} that the
    frontend can act on when the notification is tapped. It must be
    JSON-serialisable (ids / strings), since Celery serialises task args.

    `actor` / `page` (optional, kwarg-only) gate the push on the recipient's
    mutes (audit M11). If passed, this helper checks `MutedUser` /
    `MutedPage` BEFORE enqueuing the Celery task and silently returns
    when the recipient has muted the actor or the page. Callers that
    DON'T pass these params get the legacy behaviour (no mute check),
    so this extension is backward-compatible. The audit's "Question:
    is mute feed-only or feed+notifications?" resolves to
    "feed+notifications" here -- the typical product semantics. Other
    push sites can be migrated in the same shape if/when desired; the
    audit only flagged `_push_new_message` and `_notify_post_tags`.
    """
    if recipient is None:
        return
    # Tasks take serialisable args (ids, not model instances) — see tasks.py.
    recipient_id = getattr(recipient, "id", None)
    if recipient_id is None:
        return

    # M11: bail before enqueuing if the recipient has muted the actor
    # or the page. Cheap check; saves the Celery task overhead + the
    # FCM round trip for every muted-source push.
    if actor is not None or page is not None:
        actor_id = getattr(actor, "id", None) if actor is not None else None
        page_id = getattr(page, "id", None) if page is not None else None
        if _recipient_has_muted(
            recipient_id, actor_id=actor_id, page_id=page_id,
        ):
            return

    # Imported lazily to avoid a circular import at module load (tasks.py
    # imports back into utils inside its task bodies).
    from ..tasks import dispatch_push

    dispatch_push.delay(recipient_id, title, body, extra_data)


def _send_push_to_user(recipient, title, body, extra_data=None):
    """
    Synchronous worker behind `push_to_user`: do the Device lookup and the
    blocking FCM send for a single recipient. Called from the `dispatch_push`
    Celery task (and only from there) so the network round trip happens off the
    request thread.

    Why the multi-account routing data exists: a single physical device can
    have multiple accounts registered for push (a user with two accounts on the
    same phone). When a push arrives, the device needs to know which account
    it's for so the app can label it ("@bob: New follower") and route a tap to
    the right in-app session. This attaches that routing info as FCM data:

        data = {
            "for_user_id":   42,
            "for_username":  "bob",
            ...extra_data,
        }

    Reach for the lower-level `send_push_notification` only when you genuinely
    have a list of tokens but no single recipient (rare — DM groups loop and
    call per-recipient so each push carries the right for_user_id; see
    dispatch_push_to_many in tasks.py).
    """
    if recipient is None:
        return

    # Imported lazily to avoid a circular import at module load.
    from ..models import Device

    tokens = list(
        Device.objects
        .filter(user=recipient)
        .values_list("token", flat=True)
    )
    if not tokens:
        return

    data = {
        "for_user_id": recipient.id,
        "for_username": getattr(recipient, "username", "") or "",
    }
    if extra_data:
        data.update(extra_data)

    send_push_notification(tokens=tokens, title=title, body=body, data=data)


# ──────────────────────────────────────────────────────────────────────────────
# Activity logging
# ──────────────────────────────────────────────────────────────────────────────

