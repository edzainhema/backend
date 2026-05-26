"""
Batch analytics ingestion.

The mobile client buffers high-volume, fire-and-forget analytics events
(post dwell, reel watch, comment scroll, hashtag taps, shares, tab switches,
search clicks) and flushes them together instead of firing one authenticated
HTTP request per event. A single scroll session used to be dozens of
round-trips; it's now one.

The request body is:

    { "events": [ { "kind": "post_dwell", "data": { ...fields } }, ... ] }

Each event is dispatched to the same validation + log_activity logic the
per-event endpoints in analytics.py use (mirrored in the `_ingest_*` helpers
below). Per-event errors are swallowed so one malformed row can never sink the
whole batch — analytics must never be the reason a request fails.

Deliberately NOT batched: profile-visit and video-watch. Those have
enter/leave semantics and return a row id the client awaits, so they keep
their own synchronous endpoints in analytics.py.
"""

import logging
import math

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import Activity, ReelWatch
from ..utils import build_activity, bump_activity_counter

logger = logging.getLogger(__name__)


# Hard cap on events processed per request. The client flushes well below this;
# the cap purely guards against a malformed/malicious payload trying to pin a
# request open doing unbounded work.
MAX_EVENTS_PER_BATCH = 100


class _BatchAccumulator:
    """
    Collects the DB writes a batch would make so they can be flushed in bulk
    (BACKEND_SCALING_AUDIT.md SY-3).

    The per-event ingest helpers used to call log_activity directly, so a
    25-event batch became ~25 individual Activity INSERTs + 25 cache.incr round
    trips (and reel events each added a get_or_create + save). On SQLite that's
    ~25 sequential write-lock acquisitions in one request.

    Now each helper just records intent: emit() BUILDS an Activity (no INSERT
    yet); note_reel() folds a reel-watch high-water mark per post. flush() then
    does the whole batch in a handful of statements: ONE Activity.bulk_create,
    ONE ReelWatch reconcile (fetch + bulk_update/bulk_create), and ONE counter
    bump by the number of rows written.
    """

    def __init__(self, user):
        self.user = user
        self._activities = []      # unsaved Activity instances
        self._reel_seconds = {}    # post_id -> max watched seconds in this batch

    def emit(self, action_type, **kwargs):
        """Build an Activity for this event and queue it for bulk_create.
        Mirrors what log_activity would have inserted, minus the DB write."""
        act = build_activity(self.user, action_type, **kwargs)
        if act is not None:
            self._activities.append(act)

    def note_reel(self, post_id, seconds):
        """Record a reel-watch duration, keeping the per-post high-water mark
        so duplicate watch events in one batch collapse to a single upsert."""
        seconds = max(0.0, float(seconds))
        if seconds > self._reel_seconds.get(post_id, -1.0):
            self._reel_seconds[post_id] = seconds

    def flush(self):
        """Persist everything accumulated. Self-contained and best-effort: a
        failure in one sink is logged and never raised (analytics must not
        break the request path)."""
        written = 0
        if self._activities:
            try:
                Activity.objects.bulk_create(self._activities)
                written = len(self._activities)
            except Exception as exc:
                logger.error(f"[log_activity_batch] bulk_create failed: {exc}")
                written = 0

        if self._reel_seconds:
            self._flush_reel_watches()

        if written:
            # ONE increment for the whole batch instead of one cache round trip
            # per event.
            bump_activity_counter(self.user.id, written)

    def _flush_reel_watches(self):
        """Reconcile ReelWatch high-water marks for every post in the batch in
        TWO queries (fetch existing, then bulk_update) plus at most one
        bulk_create — instead of a get_or_create + save per reel event."""
        try:
            post_ids = list(self._reel_seconds.keys())
            existing = {
                rw.post_id: rw
                for rw in ReelWatch.objects.filter(
                    user=self.user, post_id__in=post_ids
                )
            }
            now = timezone.now()
            to_update = []
            to_create = []
            for post_id, seconds in self._reel_seconds.items():
                rw = existing.get(post_id)
                if rw is None:
                    to_create.append(ReelWatch(
                        user=self.user, post_id=post_id, seconds_watched=seconds,
                    ))
                elif seconds > rw.seconds_watched:
                    rw.seconds_watched = seconds
                    # updated_at is auto_now, which bulk_update does NOT trigger
                    # (auto_now only fires on .save()). Set it by hand so a
                    # high-water bump still refreshes the timestamp, matching the
                    # per-event get_or_create + save() path it replaces.
                    rw.updated_at = now
                    to_update.append(rw)
            if to_update:
                ReelWatch.objects.bulk_update(
                    to_update, ["seconds_watched", "updated_at"]
                )
            if to_create:
                # ignore_conflicts: the single-event reel endpoint may have
                # inserted the same (user, post) unique row between our fetch
                # and now; skip the dup rather than 500 the whole batch.
                ReelWatch.objects.bulk_create(to_create, ignore_conflicts=True)
        except Exception as exc:
            logger.error(f"[log_activity_batch] reel watch flush failed: {exc}")


def _surface_or(data, default):
    valid = {code for code, _ in Activity.SURFACE_CHOICES}
    s = data.get("surface")
    return s if s in valid else default


def _channel_or(data, default):
    valid = {code for code, _ in Activity.CHANNEL_CHOICES}
    c = data.get("channel")
    return c if c in valid else default


# ── Per-kind ingest helpers ────────────────────────────────────────────────
# Each mirrors the validation of its single-event counterpart in analytics.py
# and is the body the dispatcher runs. They raise on bad input; the dispatcher
# catches and skips, so a bad row never sinks the rest of the batch.

def _ingest_post_view(acc, data):
    post_id = int(data["post_id"])
    duration = float(data.get("duration_seconds", 0) or 0)
    acc.emit(
        "post_view",
        post_id=post_id,
        duration_seconds=max(0.0, duration),
        surface=_surface_or(data, "post_detail"),
        channel=_channel_or(data, "direct"),
    )


def _ingest_post_dwell(acc, data):
    post_id = int(data["post_id"])
    duration = float(data.get("duration_seconds", 0) or 0)
    acc.emit(
        "post_dwell",
        post_id=post_id,
        duration_seconds=max(0.0, duration),
        surface=_surface_or(data, "home"),
    )


def _ingest_comment_scroll(acc, data):
    post_id = int(data["post_id"])
    depth = float(data["depth"])
    if not math.isfinite(depth):
        raise ValueError("depth not finite")
    depth = min(max(depth, 0.0), 1.0)

    meta = {"depth": depth}
    for key in ("max_comments_seen", "total_comments"):
        val = data.get(key)
        if val is not None:
            try:
                meta[key] = max(0, int(val))
            except (TypeError, ValueError):
                pass

    acc.emit(
        "comment_scroll",
        post_id=post_id, surface="comments", metadata=meta,
    )


def _ingest_reel_watch(acc, data):
    post_id = int(data["post_id"])
    duration = float(data.get("duration_seconds", 0) or 0)

    # Total length of the video (NOT watch time): lets the ranker score this as
    # a completion ratio. Optional + defensively validated.
    video_seconds = None
    raw_video = data.get("video_duration_seconds")
    if raw_video is not None:
        try:
            v = float(raw_video)
        except (TypeError, ValueError):
            v = None
        if v is not None and math.isfinite(v) and v > 0:
            video_seconds = min(v, 600.0)

    watched_to_end = bool(data.get("watched_to_end", False))
    is_rewatch = bool(data.get("is_rewatch", False))
    is_skip = bool(data.get("is_skip", False))

    reel_meta = {}
    if video_seconds is not None:
        reel_meta["video_seconds"] = video_seconds

    acc.emit(
        "reel_watch",
        post_id=post_id,
        duration_seconds=max(0.0, duration),
        watched_to_end=watched_to_end,
        is_rewatch=is_rewatch,
        is_skip=is_skip,
        surface="reels",
        **({"metadata": reel_meta} if reel_meta else {}),
    )
    if watched_to_end:
        acc.emit("reel_complete", post_id=post_id, surface="reels")
    if is_rewatch:
        acc.emit("reel_rewatch", post_id=post_id, surface="reels")
    if is_skip:
        acc.emit("reel_skip", post_id=post_id, surface="reels")

    # Mirror the single-endpoint upsert so the watched-seconds high-water mark
    # stays consistent regardless of which path recorded the watch. Recorded on
    # the accumulator and reconciled in ONE bulk pass at flush time (SY-3),
    # instead of a get_or_create + save per reel event.
    acc.note_reel(post_id, max(0.0, duration))


def _ingest_search_click(acc, data):
    query = (data.get("query") or "").strip().lower()
    kind = data.get("kind")
    target_id = data.get("target_id")
    if not target_id or kind not in {"user", "page", "post"}:
        raise ValueError("kind and target_id required")
    target_id = int(target_id)

    fk = {}
    if kind == "user":
        fk["target_user_id"] = target_id
    elif kind == "page":
        fk["page_id"] = target_id
    else:  # post
        fk["post_id"] = target_id

    acc.emit(
        "search_click",
        query=query[:255], surface="search_results", channel="search", **fk,
    )


def _ingest_tab_view(acc, data):
    raw_tab = data.get("tab")
    if not isinstance(raw_tab, str):
        raise ValueError("tab required")

    VALID_TABS = {"Home", "Search", "Upload", "Messages", "Profile"}
    canon = {t.lower(): t for t in VALID_TABS}
    tab = canon.get(raw_tab.strip().lower())
    if tab is None:
        raise ValueError("unknown tab")

    kwargs = {"tab": tab}
    duration = data.get("duration_seconds")
    if duration is not None:
        try:
            d = float(duration)
        except (TypeError, ValueError):
            d = None
        if d is not None and math.isfinite(d) and d >= 0:
            kwargs["duration_seconds"] = min(d, 86400.0)

    acc.emit("tab_view", **kwargs)


def _ingest_hashtag_engage(acc, data):
    raw = data.get("hashtag")
    if not isinstance(raw, str):
        raise ValueError("hashtag required")
    tag = raw.strip().lstrip("#").strip()
    if not tag:
        raise ValueError("hashtag required")
    tag = tag[:100]

    acc.emit(
        "hashtag_engage",
        hashtag=tag, surface=_surface_or(data, "explore"),
    )


def _ingest_post_share(acc, data):
    post_id = int(data["post_id"])
    channel = (data.get("channel") or "direct").lower()
    if channel not in dict(Activity.CHANNEL_CHOICES):
        channel = "direct"
    acc.emit("post_share", post_id=post_id, channel=channel)


_DISPATCH = {
    "post_view":      _ingest_post_view,
    "post_dwell":     _ingest_post_dwell,
    "comment_scroll": _ingest_comment_scroll,
    "reel_watch":     _ingest_reel_watch,
    "search_click":   _ingest_search_click,
    "tab_view":       _ingest_tab_view,
    "hashtag_engage": _ingest_hashtag_engage,
    "post_share":     _ingest_post_share,
}


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_activity_batch(request):
    """Ingest a batch of fire-and-forget analytics events in one request.

    Body: { "events": [ { "kind": str, "data": {...} }, ... ] }
    Returns a small accepted/skipped tally; the client ignores it.
    """
    events = request.data.get("events")
    if not isinstance(events, list):
        return Response({"error": "events must be a list"}, status=400)

    # Validate each event in the loop (cheap, per-event try/except so one bad
    # row can't sink the batch), but only ACCUMULATE the resulting rows — the
    # DB writes are deferred to a single bulk flush below (SY-3).
    acc = _BatchAccumulator(request.user)
    accepted = 0
    skipped = 0
    for ev in events[:MAX_EVENTS_PER_BATCH]:
        if not isinstance(ev, dict):
            skipped += 1
            continue
        handler = _DISPATCH.get(ev.get("kind"))
        data = ev.get("data")
        if handler is None or not isinstance(data, dict):
            skipped += 1
            continue
        try:
            handler(acc, data)
            accepted += 1
        except Exception:
            # One bad event must never sink the batch.
            skipped += 1

    # ONE Activity.bulk_create + ONE ReelWatch reconcile + ONE counter bump for
    # the whole batch, instead of ~N INSERTs and N cache.incr round trips.
    acc.flush()

    return Response({"status": "ok", "accepted": accepted, "skipped": skipped})
