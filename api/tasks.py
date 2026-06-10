"""
Celery tasks for deferrable per-event work (BACKEND_SCALING_AUDIT.md INF-5).

These are the queue entry points the request / WebSocket paths call instead of
doing slow work inline:

  * dispatch_push / dispatch_push_to_many — push notifications off the request
    thread (SY-2) and off the WebSocket receive loop (WS-3).
  * process_post_media (SY-1) — HLS packaging off the upload request path.

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
#   * SY-1 — DONE (HLS). The MP4 + thumbnail are still produced synchronously in
#            _process_media_files (fast, keeps the post immediately playable),
#            but the slow HLS ladder is now packaged by process_post_media(...)
#            below, enqueued via transaction.on_commit after the post commits.
#            Because the MP4 is always present, no "processing" post state or
#            feed readiness gate is needed — hls_master simply backfills.
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
import os

from celery import shared_task
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Per-task time limits for media encoding (process_post_media). The HLS ladder
# for a long (~7-min) clip can run for several minutes — far longer than the
# quick push/notify tasks — so it gets its OWN, generous limits here instead of
# raising the global CELERY_TASK_*_TIME_LIMIT (which would let a stuck quick
# task hang for minutes too). Keep these ABOVE hls.HLS_ENCODE_TIMEOUT so
# ffmpeg's own timeout trips first with a clean log line; the Celery hard limit
# is only the backstop for a process that ignores SIGTERM. Tunable via env.
_MEDIA_SOFT_TIME_LIMIT = int(os.environ.get("MEDIA_TASK_SOFT_TIME_LIMIT", "960"))
_MEDIA_HARD_TIME_LIMIT = int(os.environ.get("MEDIA_TASK_TIME_LIMIT", "1020"))


# L7: redelivery dedup for the push tasks. With a real broker, a task can be
# REDELIVERED to a worker (acks_late + visibility-timeout expiry, or a worker
# restart mid-task) — and the redelivery carries the SAME Celery task id. We
# record a task id as "sent" AFTER the send completes and short-circuit any
# redelivery that finds it already recorded, so a redelivered task doesn't fire
# a duplicate push. Two important design choices:
#   • Key on the TASK ID, not the push content. Two genuinely-distinct pushes
#     (e.g. two real messages from the same sender in the same chat) are
#     different tasks with different ids, so they're never collapsed.
#   • Mark AFTER the send, not before. A crash mid-send leaves the key unset, so
#     the redelivery still does the work — at-least-once, never a dropped push.
#     The only residual duplicate window is a crash landing between send-return
#     and the mark, which is vanishing.
# Entirely inert in EAGER mode (current default, no broker): there is no
# redelivery, and each .delay() apply gets a fresh task id anyway.
_PUSH_DEDUP_TTL_S = 600


def _push_already_sent(task_id) -> bool:
    return bool(task_id) and cache.get(f"push_task_sent:{task_id}") is not None


def _mark_push_sent(task_id) -> None:
    if task_id:
        cache.add(f"push_task_sent:{task_id}", 1, timeout=_PUSH_DEDUP_TTL_S)


@shared_task(bind=True, ignore_result=True)
def dispatch_push(self, recipient_id, title, body, extra_data=None):
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
    if _push_already_sent(self.request.id):
        return  # L7: redelivery of an already-sent push — skip the duplicate.

    from django.contrib.auth.models import User

    from .services.push import _send_push_to_user

    try:
        recipient = User.objects.get(id=recipient_id)
    except User.DoesNotExist:
        return
    _send_push_to_user(recipient, title=title, body=body, extra_data=extra_data)
    _mark_push_sent(self.request.id)


@shared_task(bind=True, ignore_result=True)
def dispatch_push_to_many(self, recipient_ids, title, body, extra_data=None):
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
    if _push_already_sent(self.request.id):
        return  # L7: redelivery of an already-sent fan-out — skip duplicates.

    from collections import defaultdict

    from django.contrib.auth.models import User

    from .models import Device
    from .services.push import send_push_notification

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

    _mark_push_sent(self.request.id)  # L7: mark after the fan-out completes.


@shared_task(
    ignore_result=True,
    soft_time_limit=_MEDIA_SOFT_TIME_LIMIT,
    time_limit=_MEDIA_HARD_TIME_LIMIT,
)
def process_post_media(post_media_id):
    """Package one video PostMedia into an HLS ladder on a worker (SY-1).

    The MP4 (`pm.file`) and its thumbnail were already produced synchronously in
    create_post, so the post is playable the moment it's created. This task does
    only the slow, optional HLS step off the request path: read the stored MP4,
    build the adaptive ladder, upload it, and point `hls_master` at the master
    playlist. The app prefers `hls` and falls back to the MP4, so until this
    runs (or if it fails) the clip simply plays the MP4 — HLS is best-effort.

    Enqueued via `transaction.on_commit(process_post_media.delay, pm.id)` so it
    never runs before the row is committed or for a rolled-back post. In EAGER
    mode (no broker) it runs inline at commit, matching the old behaviour.

    Idempotent: returns early if `hls_master` is already set, so a redelivery
    (acks_late) or a manual re-run never double-encodes or orphans a second
    bundle. Every failure path leaves `hls_master` null and logs — the MP4
    keeps playing, so a flaky encode never affects the post.
    """
    from django.core.files.storage import default_storage

    from .models import PostMedia
    from .services.media import build_hls_ladder, store_hls_bundle

    try:
        pm = PostMedia.objects.get(id=post_media_id)
    except PostMedia.DoesNotExist:
        return  # media deleted between enqueue and run — nothing to do

    # Already packaged (redelivery / re-run) — don't encode or store again.
    if pm.hls_master:
        return

    # Pull the stored MP4 bytes (it's in S3/local storage by now).
    try:
        with default_storage.open(pm.file.name, "rb") as fh:
            video_bytes = fh.read()
    except Exception as exc:
        logger.warning("[process_post_media] %s: cannot read source: %s", post_media_id, exc)
        return

    bundle = build_hls_ladder(video_bytes)
    if bundle is None:
        # Encode failed (logged inside build_hls_ladder). Leave the MP4 as the
        # only source; it's already playable. Re-runnable later if needed.
        logger.error("[process_post_media] %s: HLS encode failed, keeping MP4", post_media_id)
        return

    try:
        master_key, _keys = store_hls_bundle(bundle)
    except Exception as exc:
        logger.error("[process_post_media] %s: HLS store failed, keeping MP4: %s", post_media_id, exc)
        return

    pm.hls_master.name = master_key
    pm.save(update_fields=["hls_master"])


@shared_task(ignore_result=True)
def notify_moderation(
    kind, report_id, target_id, target_label, reason, reporter_username,
    details="",
):
    """Escalate a freshly-filed user report to the moderation channel (H4).

    Off the request path, like the push tasks: the report row is already
    committed and visible in admin before this runs. Delegates to
    ``services.moderation.escalate_report``, which coalesces floods and swallows
    its own SES/Slack errors — so a flaky alert channel can never affect the
    report write or retry-storm the worker.
    """
    from .services.moderation import escalate_report

    escalate_report(
        kind=kind, report_id=report_id, target_id=target_id,
        target_label=target_label, reason=reason,
        reporter_username=reporter_username, details=details,
    )
