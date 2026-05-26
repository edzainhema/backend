import logging
import os
import base64
import json
import subprocess
from django.conf import settings

# firebase-admin is optional (see backend/firebase.py): it may be absent in CI,
# tests, or a fresh checkout. Guarded so importing this module never fails;
# push helpers below no-op when it isn't available.
try:
    from firebase_admin import messaging, exceptions as firebase_exceptions
except ImportError:  # pragma: no cover - exercised only without the SDK installed
    messaging = None
    firebase_exceptions = None


# ---------------------------------------------------------------------------
# Keyset (cursor) pagination helpers
# ---------------------------------------------------------------------------
# A cursor is just an opaque, base64-encoded JSON object carrying the sort-key
# values of the last row on the previous page (e.g. {"created_at": "...",
# "id": 42}). Keyset pagination compares against those values instead of using
# OFFSET, so it stays fast at any depth and never skips/duplicates rows when
# items are inserted or removed between requests (important for lists the user
# mutates, like the followers screen's "remove" action).
#
# Both helpers are deliberately defensive: a malformed/legacy/garbage cursor
# decodes to {} (treated as "start from the beginning") rather than raising,
# so a stale client token can never 500 the endpoint.

def encode_cursor(payload: dict) -> str:
    """Serialise a sort-key dict into an opaque, URL-safe cursor token."""
    raw = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(token) -> dict:
    """Decode a cursor token back into a dict. Returns {} on any bad input."""
    if not token:
        return {}
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ── Presence: throttled last_seen write (WS-2) ──────────────────────────────
# last_seen otherwise gets written on every authenticated HTTP request
# (UpdateLastSeenMiddleware) and every inbound WebSocket frame (the chat
# consumers) — a write to the hot UserProfile row on essentially every user
# action. We throttle it behind a short-lived cache marker so the DB write
# happens at most once per LAST_SEEN_THROTTLE_S per user. is_online uses a
# 3-minute window, so a marker up to ~45s old still keeps presence accurate.
LAST_SEEN_THROTTLE_S = 45


def touch_last_seen(user_id):
    """Best-effort, throttled bump of UserProfile.last_seen for ``user_id``.

    Uses ``cache.add()`` (atomic check-and-set): it returns True only for the
    first caller within the TTL window, so exactly one DB write happens per
    window and concurrent callers can't race into duplicate writes the way a
    ``get()`` + ``set()`` pair can. Never raises — presence must never break a
    request or a WebSocket frame.
    """
    if not user_id:
        return
    try:
        from django.core.cache import cache

        # add() succeeds (True) only when no marker exists yet → do the write
        # and open the window. While the marker lives, every later call is a
        # single cache hit with no DB write.
        if cache.add(f"last_seen:{user_id}", 1, timeout=LAST_SEEN_THROTTLE_S):
            from django.utils import timezone

            from .models import UserProfile

            UserProfile.objects.filter(user_id=user_id).update(
                last_seen=timezone.now()
            )
    except Exception:
        pass  # presence is best-effort — never break the caller


def compress_video(input_path):
    # Create output path
    filename = os.path.basename(input_path)
    output_path = os.path.join(settings.MEDIA_ROOT, "uploads/compressed", f"compressed_{filename}")

    # Ensure directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # FFmpeg compression command (H.264)
    command = [
        "ffmpeg", "-i", input_path,
        "-vcodec", "libx264",
        "-crf", "28",               # Compression level (lower = better quality)
        "-preset", "veryfast",      # Change to "slow" for better compression
        "-acodec", "aac",
        "-b:a", "128k",
        output_path
    ]

    # Run FFmpeg
    subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    return output_path

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
        from .models import Device
        deleted, _ = Device.objects.filter(token__in=dead_tokens).delete()
        logger.info(f"[push] pruned {deleted} dead Device row(s)")

    logger.info(
        f"[push] sent ok={response.success_count} "
        f"failed={response.failure_count} total={len(tokens)}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Per-user push helper (multi-account aware)
# ──────────────────────────────────────────────────────────────────────────────

def push_to_user(recipient, title, body, extra_data=None):
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
    """
    if recipient is None:
        return
    # Tasks take serialisable args (ids, not model instances) — see tasks.py.
    recipient_id = getattr(recipient, "id", None)
    if recipient_id is None:
        return

    # Imported lazily to avoid a circular import at module load (tasks.py
    # imports back into utils inside its task bodies).
    from .tasks import dispatch_push

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
    from .models import Device

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

def build_activity(user, action_type, **kwargs):
    """
    Construct (but do NOT save) an Activity row from loosely-typed kwargs.

    This is the shared field-mapping core of log_activity, split out so the
    batch-ingest endpoint can accumulate many instances and persist them with a
    single Activity.objects.bulk_create (BACKEND_SCALING_AUDIT.md SY-3) instead
    of one INSERT per event. Pure construction — it touches neither the DB nor
    the cache — so the caller decides when, and how many rows, to write.

    Returns an unsaved Activity instance, or None when `user` isn't an
    authenticated user (the same guard log_activity has always had). Unknown
    kwargs are folded into `metadata`, and the current request's session id is
    attached when the caller didn't pass one — identical to the original logic.
    """
    # Imported lazily so utils.py stays importable before app-loading is done.
    from .models import Activity

    if user is None or not getattr(user, "is_authenticated", False):
        return None

    valid_fields = {
        "post", "page", "target_user", "comment",
        # FK ID forms — Django accepts post_id, page_id, etc. directly
        "post_id", "page_id", "target_user_id", "comment_id",
        "duration_seconds", "surface", "channel", "tab",
        "watched_to_end", "is_rewatch", "is_skip",
        "query", "sentiment_label", "sentiment_score",
        "keywords", "hashtag", "metadata", "session_id",
    }

    direct = {k: v for k, v in kwargs.items() if k in valid_fields}
    extras = {k: v for k, v in kwargs.items() if k not in valid_fields}
    if extras:
        meta = direct.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        meta.update(extras)
        direct["metadata"] = meta

    # C3: tag the row with the current request's session id (set by
    # SessionIdMiddleware from the X-Session-Id header) unless the caller
    # passed one explicitly. This groups every event from one visit without
    # having to thread session_id through each view.
    if not direct.get("session_id"):
        try:
            from .session_context import get_current_session_id
            sid = get_current_session_id()
            if sid:
                direct["session_id"] = sid
        except Exception:
            pass

    return Activity(user=user, action_type=action_type, **direct)


def bump_activity_counter(user_id, n=1):
    """
    Increment the per-user activity counter (feed:activity_count:<id>) by `n`.

    The feed uses this counter to rebuild the cached activity profile once the
    viewer has logged 20+ new events since the last build, instead of waiting
    out the 30-minute TTL (docs/FEED_RANKING_SPEC.md:247). Power users (50+
    events in 5 min) were otherwise waiting up to 30 min for their feed to
    adapt; this closes that gap.

    `cache.incr` is atomic on Redis (the project's cache backend) but raises
    ValueError if the key doesn't exist yet — the standard idiom is try/except
    with a set() bootstrap. Expiry is generous (24h): it only needs to outlast
    a session, and _build_activity_profile snapshots the value on every rebuild.
    Best-effort — a cache outage must NEVER break the analytics write.

    Splitting this out lets the batch endpoint bump ONCE by the number of rows
    written, rather than paying a cache round trip per event (SY-3).
    """
    if n <= 0:
        return
    try:
        from django.core.cache import cache
        counter_key = f"feed:activity_count:{user_id}"
        try:
            cache.incr(counter_key, n)
        except ValueError:
            cache.set(counter_key, n, timeout=86400)
    except Exception as cache_exc:
        logger.warning(f"[log_activity] counter bump failed: {cache_exc}")


def log_activity(user, action_type, **kwargs):
    """
    Best-effort single-event activity write: build the row, INSERT it, and bump
    the per-user counter by 1.

    Never raises — analytics must never break the request path. Unknown kwargs
    are dropped into `metadata` so callers don't have to keep the schema in
    their head.

    For high-volume batched ingest, prefer build_activity + bulk_create +
    bump_activity_counter(n) so N events cost one INSERT and one counter bump
    instead of N of each — see api/views/activity_batch.py (SY-3).

    Usage:
        log_activity(request.user, "post_like", post=post, surface="home")
        log_activity(request.user, "search_query", query="sunset")
    """
    activity = build_activity(user, action_type, **kwargs)
    if activity is None:
        return None

    try:
        activity.save()
    except Exception as exc:
        # Analytics writes must never break the user request.
        logger.error(f"[log_activity] failed: {exc}")
        return None

    bump_activity_counter(user.id, 1)
    return activity


# ──────────────────────────────────────────────────────────────────────────────
# Hashtag indexing
# ──────────────────────────────────────────────────────────────────────────────

# Hard cap on hashtags persisted per post. A description can technically
# contain hundreds of "#" tokens; storing them all is pointless (a post
# spamming 200 tags is not "about" 200 topics) and would bloat the index.
# 30 is comfortably above any genuine caption.
_MAX_HASHTAGS_PER_POST = 30


def sync_post_hashtags(post):
    """
    Reconcile a post's PostHashtag rows with the hashtags currently in its
    description. Idempotent and diff-based: safe to call on create and on
    any future edit path — it only inserts genuinely-new tags and deletes
    tags that are no longer present.

    Best-effort: never raises into the request path. Hashtag indexing is
    a ranking optimisation, not a correctness requirement, so a failure
    here must not break post creation. (Mirrors log_activity's contract.)

    Returns the set of tags now stored for the post, or None on failure.
    """
    from .models import PostHashtag
    from .comment_analyzer import extract_hashtags

    try:
        # extract_hashtags returns lowercased, de-duplicated tags without
        # the leading '#'. Clamp each to the column width and cap the count.
        wanted = {
            t[:100] for t in extract_hashtags(post.description or "")
        }
        if len(wanted) > _MAX_HASHTAGS_PER_POST:
            # Deterministic truncation: sort so the same post always keeps
            # the same subset rather than relying on set iteration order.
            wanted = set(sorted(wanted)[:_MAX_HASHTAGS_PER_POST])

        existing = set(
            PostHashtag.objects
            .filter(post=post)
            .values_list("hashtag", flat=True)
        )

        to_add = wanted - existing
        to_remove = existing - wanted

        if to_remove:
            PostHashtag.objects.filter(
                post=post, hashtag__in=to_remove
            ).delete()

        if to_add:
            PostHashtag.objects.bulk_create(
                [PostHashtag(post=post, hashtag=t) for t in to_add],
                ignore_conflicts=True,
            )

        return wanted
    except Exception as exc:
        post_id = getattr(post, "id", None)
        logger.error(f"[sync_post_hashtags] failed for post {post_id}: {exc}")
        return None
