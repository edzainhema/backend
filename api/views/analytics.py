import math


from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response



from ..models import (
    Activity, Page, Post, ProfileVisit, ReelWatch, VideoWatch,
)
from ..utils import log_activity


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_profile_visit(request):
    visitor = request.user

    visit_id = request.data.get("visit_id")
    duration_seconds = request.data.get("duration_seconds")

    if visit_id is not None and duration_seconds is not None:
        try:
            visit = ProfileVisit.objects.get(
                id=int(visit_id),
                visitor=visitor,
            )
        except (ProfileVisit.DoesNotExist, ValueError):
            return Response({"error": "Visit not found"}, status=404)

        visit.duration_seconds = max(0, int(duration_seconds))
        visit.save(update_fields=["duration_seconds"])

        if visit.visited_user_id:
            log_activity(
                visitor,
                "user_visit",
                target_user_id=visit.visited_user_id,
                duration_seconds=visit.duration_seconds,
                channel=visit.channel,
                surface=visit.surface,
            )
        elif visit.visited_page_id:
            log_activity(
                visitor,
                "page_visit",
                page_id=visit.visited_page_id,
                duration_seconds=visit.duration_seconds,
                channel=visit.channel,
                surface=visit.surface,
            )

        return Response({"status": "updated"})

    user_id = request.data.get("user_id")
    page_id = request.data.get("page_id")
    # Derive the allowlist from the model itself so this view can't drift
    # from ProfileVisit.SURFACE_CHOICES. Previously the inline literal was
    # narrower than the model (missing search / search_results / history)
    # and silently coerced perfectly valid client values to "profile",
    # which made downstream analytics on those surfaces always return zero.
    channel = request.data.get("channel", "direct")
    valid_channels = {code for code, _ in ProfileVisit.CHANNEL_CHOICES}
    if channel not in valid_channels:
        channel = "direct"
    surface = request.data.get("surface", "profile")
    valid_surfaces = {code for code, _ in ProfileVisit.SURFACE_CHOICES}
    if surface not in valid_surfaces:
        surface = "profile"

    if user_id:
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return Response({"error": "Invalid user_id"}, status=400)
        if user_id == visitor.id:
            return Response({"status": "ignored"})
        target_user = get_object_or_404(User, id=user_id)
        visit = ProfileVisit.objects.create(
            visitor=visitor,
            visited_user=target_user,
            channel=channel,
            surface=surface,
        )
    elif page_id:
        try:
            page_id = int(page_id)
        except (TypeError, ValueError):
            return Response({"error": "Invalid page_id"}, status=400)
        page = get_object_or_404(Page, id=page_id)
        visit = ProfileVisit.objects.create(
            visitor=visitor,
            visited_page=page,
            channel=channel,
            surface=surface,
        )
    else:
        return Response({"error": "user_id or page_id required"}, status=400)

    return Response({"status": "logged", "visit_id": visit.id})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_video_watch(request):
    viewer = request.user

    watch_id = request.data.get("watch_id")
    duration_seconds = request.data.get("duration_seconds")

    if watch_id is not None and duration_seconds is not None:
        try:
            watch = VideoWatch.objects.get(
                id=int(watch_id),
                viewer=viewer,
            )
        except (VideoWatch.DoesNotExist, ValueError):
            return Response({"error": "Watch not found"}, status=404)

        watch.duration_seconds = max(0, int(duration_seconds))
        watch.save(update_fields=["duration_seconds"])

        return Response({"status": "updated"})

    post_id = request.data.get("post_id")
    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return Response({"error": "Invalid post_id"}, status=400)

    post = get_object_or_404(Post, id=post_id)

    channel = request.data.get("channel", "direct")
    if channel not in {"direct", "link", "search"}:
        channel = "direct"
    surface = request.data.get("surface", "reels")
    if surface not in {"home", "explore", "profile", "page", "reels", "search", "search_results"}:
        surface = "reels"

    watch = VideoWatch.objects.create(
        viewer=viewer,
        post=post,
        channel=channel,
        surface=surface,
    )

    return Response({"status": "logged", "watch_id": watch.id})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_post_view(request):
    """Body: post_id, duration_seconds, surface (optional), channel (optional)."""
    post_id = request.data.get("post_id")
    duration = request.data.get("duration_seconds", 0)
    surface = request.data.get("surface", "post_detail")
    channel = request.data.get("channel", "direct")

    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    try:
        post_id = int(post_id)
        duration = float(duration)
    except (TypeError, ValueError):
        return Response({"error": "invalid input"}, status=400)

    log_activity(
        request.user,
        "post_view",
        post_id=post_id,
        duration_seconds=max(0.0, duration),
        surface=surface if surface in dict(Activity.SURFACE_CHOICES) else "post_detail",
        channel=channel if channel in dict(Activity.CHANNEL_CHOICES) else "direct",
    )
    return Response({"status": "logged"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_post_dwell(request):
    """Body: post_id, duration_seconds, surface."""
    post_id = request.data.get("post_id")
    duration = request.data.get("duration_seconds", 0)
    surface = request.data.get("surface", "home")

    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    try:
        post_id = int(post_id)
        duration = float(duration)
    except (TypeError, ValueError):
        return Response({"error": "invalid input"}, status=400)

    log_activity(
        request.user,
        "post_dwell",
        post_id=post_id,
        duration_seconds=max(0.0, duration),
        surface=surface if surface in dict(Activity.SURFACE_CHOICES) else "home",
    )
    return Response({"status": "logged"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_comment_scroll(request):
    """
    Body: post_id (required), depth (required, 0..1 — how far down the loaded
    comment thread the viewer scrolled), max_comments_seen / total_comments
    (optional ints, kept for analytics).

    Records a `comment_scroll` Activity row. Reading deep into a post's
    comments is a strong "this post is genuinely interesting" signal that's
    independent of whether the viewer liked it (ACTIVITY_AND_FEED_AUDIT.md
    item D1). The feed ranker reads the depth fraction from metadata and
    scales the credit by it, so a near-complete read counts strongly while a
    shallow glance is dropped as noise.

    High-volume + fire-and-forget on the client, so we coerce / clamp bad
    inputs rather than 400-ing where we reasonably can.
    """
    post_id = request.data.get("post_id")
    if not post_id:
        return Response({"error": "post_id required"}, status=400)
    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return Response({"error": "invalid post_id"}, status=400)

    # Depth is the core signal. Require a finite number; clamp to [0, 1].
    raw_depth = request.data.get("depth")
    try:
        depth = float(raw_depth)
    except (TypeError, ValueError):
        return Response({"error": "invalid depth"}, status=400)
    if not math.isfinite(depth):
        return Response({"error": "invalid depth"}, status=400)
    depth = min(max(depth, 0.0), 1.0)

    # Optional raw counts — purely for analytics; defensively coerced and
    # never allowed to break the write.
    meta = {"depth": depth}
    for key in ("max_comments_seen", "total_comments"):
        val = request.data.get(key)
        if val is not None:
            try:
                meta[key] = max(0, int(val))
            except (TypeError, ValueError):
                pass

    log_activity(
        request.user,
        "comment_scroll",
        post_id=post_id,
        surface="comments",
        metadata=meta,
    )
    return Response({"status": "logged"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_reel_watch(request):
    """Body: post_id, duration_seconds, video_duration_seconds (optional),
    watched_to_end, is_rewatch, is_skip."""
    post_id = request.data.get("post_id")
    duration = request.data.get("duration_seconds", 0)

    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    try:
        post_id = int(post_id)
        duration = float(duration)
    except (TypeError, ValueError):
        return Response({"error": "invalid input"}, status=400)

    # Total length of the video (NOT the watch time). Lets the feed ranker
    # score this watch as a completion ratio (watch / length) instead of
    # against an arbitrary fixed reference. Optional + defensively validated:
    # a missing / junk / non-positive value is simply not stored, and the
    # ranker falls back to its legacy reference for those rows.
    video_seconds = None
    raw_video = request.data.get("video_duration_seconds")
    if raw_video is not None:
        try:
            v = float(raw_video)
        except (TypeError, ValueError):
            v = None
        if v is not None and math.isfinite(v) and v > 0:
            # Cap at the 5-minute upload ceiling (+ slack) so a garbage
            # payload can't store a wild length.
            video_seconds = min(v, 600.0)

    watched_to_end = bool(request.data.get("watched_to_end", False))
    is_rewatch = bool(request.data.get("is_rewatch", False))
    is_skip = bool(request.data.get("is_skip", False))

    reel_meta = {}
    if video_seconds is not None:
        reel_meta["video_seconds"] = video_seconds

    log_activity(
        request.user,
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
        log_activity(request.user, "reel_complete", post_id=post_id, surface="reels")
    if is_rewatch:
        log_activity(request.user, "reel_rewatch", post_id=post_id, surface="reels")
    if is_skip:
        log_activity(request.user, "reel_skip", post_id=post_id, surface="reels")

    try:
        rw, _ = ReelWatch.objects.get_or_create(
            user=request.user,
            post_id=post_id,
            defaults={"seconds_watched": max(0.0, duration)},
        )
        rw.seconds_watched = max(rw.seconds_watched, max(0.0, duration))
        rw.save(update_fields=["seconds_watched", "updated_at"])
    except Exception:
        pass

    return Response({"status": "logged"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_search_click(request):
    """Body: query, kind ("user"|"page"|"post"), target_id."""
    # Lowercase the query for the ranking signal so case variants of the same
    # search collapse into one term (ACTIVITY_AND_FEED_AUDIT.md item A15).
    query = (request.data.get("query") or "").strip().lower()
    kind = request.data.get("kind")
    target_id = request.data.get("target_id")

    if not target_id or kind not in {"user", "page", "post"}:
        return Response({"error": "kind and target_id required"}, status=400)

    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        return Response({"error": "invalid target_id"}, status=400)

    fk_kwargs = {}
    if kind == "user":
        fk_kwargs["target_user_id"] = target_id
    elif kind == "page":
        fk_kwargs["page_id"] = target_id
    elif kind == "post":
        fk_kwargs["post_id"] = target_id

    log_activity(
        request.user,
        "search_click",
        query=query[:255],
        surface="search_results",
        channel="search",
        **fk_kwargs,
    )
    return Response({"status": "logged"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_tab_view(request):
    """
    Body: tab (string, required), duration_seconds (optional float).

    Records a `tab_view` Activity row whenever the user switches between
    the top-level tabs. The frontend fires this on every tab transition
    (see navigation/MainTabs.tsx:119), so the volume is high — we keep
    the validation cheap and silently coerce bad inputs rather than 400.
    No downstream ranking consumer reads this signal yet; the data is
    captured now so product analytics can mine it later (which tabs do
    new users open first, etc.).
    """
    raw_tab = request.data.get("tab")
    if not isinstance(raw_tab, str):
        return Response({"error": "tab required"}, status=400)

    # Whitelist against the five real tabs declared in MainTabs.tsx:205-209.
    # Anything else is almost certainly a typo or a renamed-but-unsynced
    # tab; bouncing it keeps the table clean.
    #
    # Match case-INsensitively and store the canonical capitalized form.
    # The client sends lowercased names (MainTabs.tsx calls
    # logTabView({ tab: targetName.toLowerCase() })), so the previous exact
    # capitalized comparison 400-rejected EVERY tab_view event — meaning the
    # tab-dwell signal the feed ranker's discovery-appetite uses (B1) was
    # never being recorded at all. Canonicalizing here unblocks it.
    VALID_TABS = {"Home", "Search", "Upload", "Messages", "Profile"}
    _tab_canon = {t.lower(): t for t in VALID_TABS}
    tab = _tab_canon.get(raw_tab.strip().lower())
    if tab is None:
        return Response({"error": "unknown tab"}, status=400)

    # Optional duration. Must be a finite non-negative number; we cap at
    # 24h to prevent garbage clock-skew payloads from polluting the table.
    duration = request.data.get("duration_seconds")
    if duration is not None:
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = None
        else:
            import math
            if not math.isfinite(duration) or duration < 0:
                duration = None
            else:
                duration = min(duration, 86400.0)

    kwargs = {"tab": tab}
    if duration is not None:
        kwargs["duration_seconds"] = duration

    log_activity(request.user, "tab_view", **kwargs)
    return Response({"status": "logged"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_hashtag_engagement(request):
    """
    Body: hashtag (string, with or without leading '#'), surface (optional).

    Records a `hashtag_engage` Activity row so the feed-ranking system can
    build hashtag-affinity scores (see api.feed.affinity, weight 3.0).
    The comments code already logs the same action_type whenever a posted
    comment contains a hashtag (views/comments.py:423); this endpoint is
    the explicit tap path from explore/search/post-detail surfaces.
    """
    raw = request.data.get("hashtag")
    if not isinstance(raw, str):
        return Response({"error": "hashtag required"}, status=400)

    # Normalize: strip whitespace, drop a single leading '#', cap length to
    # the Activity model's max_length so we never raise on overlong input.
    tag = raw.strip().lstrip("#").strip()
    if not tag:
        return Response({"error": "hashtag required"}, status=400)
    tag = tag[:100]

    # Constrain surface to the choices the Activity model accepts. Anything
    # the client sends that isn't recognized falls back to "explore" — that
    # matches the frontend default in utils/activity.ts:131.
    valid_surfaces = {code for code, _ in Activity.SURFACE_CHOICES}
    surface = request.data.get("surface")
    if surface not in valid_surfaces:
        surface = "explore"

    log_activity(
        request.user,
        "hashtag_engage",
        hashtag=tag,
        surface=surface,
    )
    return Response({"status": "logged"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_post_share(request):
    """Body: post_id, channel (optional)."""
    post_id = request.data.get("post_id")
    channel = (request.data.get("channel") or "direct").lower()
    if channel not in dict(Activity.CHANNEL_CHOICES):
        channel = "direct"

    if not post_id:
        return Response({"error": "post_id required"}, status=400)

    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return Response({"error": "invalid post_id"}, status=400)

    log_activity(request.user, "post_share", post_id=post_id, channel=channel)
    return Response({"status": "logged"})
