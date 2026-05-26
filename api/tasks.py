"""
Celery tasks for deferrable per-event work (BACKEND_SCALING_AUDIT.md INF-5).

These are the queue entry points the request / WebSocket paths call instead of
doing slow work inline:

  * dispatch_push / dispatch_push_to_many — push notifications off the request
    thread (SY-2) and off the WebSocket receive loop (WS-3).
  * process_post_media (SY-1) will be added here when media transcoding is
    moved out of create_post.

Task arguments are JSON-serialisable primitives (ids, strings, dicts) — we pass
a user *id*, never a model instance, and re-fetch inside the task. With no
broker configured the project runs in eager mode
(settings.CELERY_TASK_ALWAYS_EAGER), so `.delay(...)` executes inline and these
are safe to call everywhere today; they become truly asynchronous once a broker
+ worker are running.
"""

# ---------------------------------------------------------------------------
# ACTIVATION + REMAINING WIRING (TODO) — read before extending this module.
#
# To activate the queue in production:
#   1. pip install -r requirements.txt            (installs celery[redis])
#   2. Set REDIS_URL (or CELERY_BROKER_URL to a dedicated DB index, e.g.
#      redis://host:6379/2). Without it the app stays in EAGER mode and these
#      tasks run inline — see settings.py CELERY_TASK_ALWAYS_EAGER.
#   3. Run a worker:  systemctl enable --now celery-worker
#      (deploy/systemd/celery-worker.service)  — or  celery -A backend worker
#
# Status of the migrations that move work off the request / WebSocket path:
#   * WS-3 — DONE. api/consumers.py fans per-message push out via
#            dispatch_push_to_many(...) (see _enqueue_push_fanout there), so
#            the FCM round trips leave the WebSocket receive loop.
#   * SY-1 — PARTIALLY DONE. The FFmpeg / Pillow transcode no longer runs
#            inside create_post's DB transaction: api/views/posts/create.py
#            now processes all media in _process_media_files(...) BEFORE the
#            atomic() block, which then only does fast row writes. That alone
#            removes the critical "connection + SQLite write lock held open
#            for the whole transcode" problem.
#            STILL OPEN (optional, needs the broker live): to also free the
#            *request worker* during the transcode, add a process_post_media(
#            post_id) task here, stage the raw upload + editor metadata in a
#            short transaction with the Post in a "processing" state, return
#            202 immediately, and have the task transcode, attach the
#            thumbnail/dimensions, fire the mention notifications + feed-cache
#            invalidation, then flip the post to "ready". That last step also
#            needs a readiness gate in post_visibility_q (+ the home-feed rule)
#            and a frontend processing-state contract, so it's a follow-up.
#   * SY-2 — DONE. push_to_user (api/utils.py) is now an enqueue wrapper that
#            calls dispatch_push.delay(...); the Device query + blocking FCM
#            send live in _send_push_to_user, which this task invokes on a
#            worker. Every REST call site (like / comment / follow / mention /
#            page actions, DM media) became async with no call-site edits, and
#            the notification ROW writes stay inline at those call sites.
#            WHEN ENABLING THE BROKER: consider wrapping the enqueue in
#            transaction.on_commit so a push isn't sent for an action whose
#            surrounding transaction later rolls back. It's left as a plain
#            .delay() for now because in EAGER mode that exactly preserves the
#            old inline timing (and on_commit callbacks don't fire inside
#            TestCase's rolled-back transactions), so behaviour is unchanged
#            until a real broker is live.
# See BACKEND_SCALING_AUDIT.md for the full write-ups.
# ---------------------------------------------------------------------------
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def dispatch_push(recipient_id, title, body, extra_data=None):
    """Send one user's push notification from a worker (SY-2).

    This is what push_to_user enqueues: re-fetch the recipient by id (tasks take
    serialisable args, not model instances) and delegate to _send_push_to_user,
    the synchronous worker that handles the Device lookup, FCM multicast,
    multi-account routing, and dead-token pruning. A recipient deleted between
    enqueue and run is a silent no-op.

    NOTE: we call _send_push_to_user, NOT push_to_user. push_to_user is now the
    *enqueue* wrapper (it calls dispatch_push.delay), so calling it here would
    recurse — under a real broker it would spawn tasks forever, and in eager
    mode it would blow the stack. Always invoke the sync worker from here.
    """
    from django.contrib.auth.models import User

    from .utils import _send_push_to_user

    try:
        recipient = User.objects.get(id=recipient_id)
    except User.DoesNotExist:
        return
    _send_push_to_user(recipient, title=title, body=body, extra_data=extra_data)


@shared_task(ignore_result=True)
def dispatch_push_to_many(recipient_ids, title, body, extra_data=None):
    """Fan a push out to many recipients from a worker (WS-3).

    Replaces the consumer receive loop's N sequential FCM round trips with a
    single background task that:
      * looks up every recipient's devices in ONE query
        (Device.objects.filter(user_id__in=...)) instead of one query per
        recipient; and
      * still sends each recipient their OWN push carrying their for_user_id, so
        a phone with multiple accounts routes the notification to the right
        in-app account (the per-recipient routing the DM / group-chat path
        relies on -- a single shared multicast can't do that). Each recipient's
        devices go out in one multicast, so it's one FCM call per recipient, not
        per device.
    """
    from collections import defaultdict

    from django.contrib.auth.models import User

    from .models import Device
    from .utils import send_push_notification

    ids = [r for r in (recipient_ids or []) if r]
    if not ids:
        return

    # ONE query for every recipient's device tokens, grouped by user.
    tokens_by_user = defaultdict(list)
    for uid, token in (
        Device.objects.filter(user_id__in=ids).values_list("user_id", "token")
    ):
        if token:
            tokens_by_user[uid].append(token)
    if not tokens_by_user:
        return

    # ONE query for the display usernames (the for_username label).
    usernames = dict(
        User.objects.filter(id__in=tokens_by_user.keys())
        .values_list("id", "username")
    )

    base_extra = extra_data or {}
    for uid, tokens in tokens_by_user.items():
        # Per-recipient data so a multi-account device routes correctly; mirrors
        # what push_to_user attaches for a single recipient.
        data = {
            "for_user_id": uid,
            "for_username": usernames.get(uid, "") or "",
            **base_extra,
        }
        try:
            send_push_notification(tokens=tokens, title=title, body=body, data=data)
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("[dispatch_push_to_many] send to %s failed: %s", uid, exc)
