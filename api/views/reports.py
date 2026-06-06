

from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response



from ..models import (
    BlockedUser, Page, PageReport, Post, PostReport, UserReport,
)
from ..services.moderation import enqueue_report_escalation
from ..services.throttles import ReportRateThrottle


def _clean_reason(reason, reason_choices):
    """Validate a report `reason` against a model's REPORT_REASONS.

    Returns the stripped reason if it's one of the declared choice codes, else
    None. DRF function views don't run model `full_clean`, so the
    ``choices=REPORT_REASONS`` constraint is NOT enforced at save time — without
    this an arbitrary/empty string (or one >30 chars, which raises DataError →
    500 on Postgres) would land uncategorisable in the moderation queue.

    Shared by all three report views so they can't drift: the bug this fixes was
    exactly report_post validating while report_user/report_page didn't (M3).
    """
    cleaned = (reason or "").strip()
    valid = {code for code, _ in reason_choices}
    return cleaned if cleaned in valid else None


def _invalid_reason_response(reason_choices):
    return Response(
        {
            "error": "Invalid reason",
            "allowed": sorted(code for code, _ in reason_choices),
        },
        status=400,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ReportRateThrottle])
def report_post(request):
    post_id = request.data.get("post_id")

    if not post_id:
        return Response(
            {"error": "post_id required"},
            status=400
        )

    # Validate `reason` against the model's declared choices (see _clean_reason).
    reason = _clean_reason(request.data.get("reason"), PostReport.REPORT_REASONS)
    if reason is None:
        return _invalid_reason_response(PostReport.REPORT_REASONS)

    post = get_object_or_404(Post, id=post_id)
    post_owner = post.user

    # --------------------------------------------------
    # 🚫 BLOCK CHECK (REPORTER ↔ POST OWNER)
    # --------------------------------------------------
    if BlockedUser.objects.between(request.user, post_owner).exists():
        return Response(
            {"error": "Not allowed"},
            status=403
        )

    # --------------------------------------------------
    # 🚫 PREVENT SELF-REPORTING
    # --------------------------------------------------
    if post_owner == request.user:
        return Response(
            {"error": "Cannot report your own post"},
            status=400
        )

    # --------------------------------------------------
    # 📝 CREATE REPORT (dedupe race-safely)
    # --------------------------------------------------
    # L2: get_or_create instead of exists()-then-create(). The old check-then-
    # create raced on a double-tap — given unique_together('reporter','post')
    # the second concurrent insert raised IntegrityError → 500. get_or_create
    # collapses that to a clean "Already reported" 400 (matching report_user /
    # report_page, which already use it).
    report, created = PostReport.objects.get_or_create(
        reporter=request.user,
        post=post,
        defaults={"reason": reason},
    )
    if not created:
        return Response(
            {"error": "Already reported"},
            status=400
        )

    # H4: actively escalate (off the request path; best-effort). Coalesced per
    # target so a brigade can't flood the channel.
    enqueue_report_escalation(
        kind="post",
        report_id=report.id,
        reason=reason,
        target_id=post.id,
        target_label=f"post #{post.id} by @{post_owner.username}",
        reporter_username=request.user.username,
    )

    return Response(
        {
            "status": "reported",
            "report_id": report.id
        },
        status=201
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ReportRateThrottle])
def report_user(request):
    reported_user_id = request.data.get("user_id")
    details = request.data.get("details", "")

    if not reported_user_id:
        return Response(
            {"error": "user_id and reason required"},
            status=400
        )

    # M3: validate `reason` against UserReport's declared choices — the same
    # guard report_post applies. Was previously a bare truthy check, so any
    # string (or one >30 chars → DataError 500 on Postgres) was persisted.
    reason = _clean_reason(request.data.get("reason"), UserReport.REPORT_REASONS)
    if reason is None:
        return _invalid_reason_response(UserReport.REPORT_REASONS)

    # L1: coerce the id to int up front. A non-numeric `user_id` ("abc") used to
    # reach `int(...)` here and raise ValueError → unhandled 500; the same bad
    # value would then also blow up the get_object_or_404 PK lookup below. Parse
    # once, 400 on failure, and reuse the int for both the self-check and the
    # fetch so neither can ValueError.
    try:
        reported_user_id = int(reported_user_id)
    except (TypeError, ValueError):
        return Response(
            {"error": "Invalid user_id"},
            status=400
        )

    if reported_user_id == request.user.id:
        return Response(
            {"error": "Cannot report yourself"},
            status=400
        )

    reported_user = get_object_or_404(
        User,
        id=reported_user_id
    )

    # 🚫 Block check (optional but recommended)
    if BlockedUser.objects.between(request.user, reported_user).exists():
        return Response(
            {"error": "Not allowed"},
            status=403
        )

    report, created = UserReport.objects.get_or_create(
        reporter=request.user,
        reported_user=reported_user,
        defaults={
            "reason": reason,
            "details": details,
        }
    )

    if not created:
        return Response(
            {"error": "You already reported this user"},
            status=400
        )

    # H4: actively escalate (off the request path; best-effort).
    enqueue_report_escalation(
        kind="user",
        report_id=report.id,
        reason=report.reason,
        target_id=reported_user.id,
        target_label=f"user @{reported_user.username}",
        reporter_username=request.user.username,
        details=report.details or "",
    )

    return Response(
        {"status": "reported"},
        status=201
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ReportRateThrottle])
def report_page(request):
    page_id = request.data.get("page_id")
    details = request.data.get("details", "").strip()

    if not page_id:
        return Response(
            {"error": "page_id and reason are required"},
            status=400
        )

    # M3: validate `reason` against PageReport's declared choices — the same
    # guard report_post applies. Was previously a bare truthy check.
    reason = _clean_reason(request.data.get("reason"), PageReport.REPORT_REASONS)
    if reason is None:
        return _invalid_reason_response(PageReport.REPORT_REASONS)

    page = get_object_or_404(Page, id=page_id)

    report, created = PageReport.objects.get_or_create(
        reporter=request.user,
        page=page,
        defaults={
            "reason": reason,
            "details": details
        }
    )

    if not created:
        return Response(
            {"error": "You already reported this page"},
            status=400
        )

    # H4: actively escalate (off the request path; best-effort).
    enqueue_report_escalation(
        kind="page",
        report_id=report.id,
        reason=report.reason,
        target_id=page.id,
        target_label=f"page '{page.name}' (#{page.id})",
        reporter_username=request.user.username,
        details=report.details or "",
    )

    return Response(
        {"status": "reported"},
        status=201
    )
