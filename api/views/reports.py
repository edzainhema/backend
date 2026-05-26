

from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response



from ..models import (
    BlockedUser, Page, PageReport, Post, PostReport, UserReport,
)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def report_post(request):
    post_id = request.data.get("post_id")
    reason = request.data.get("reason", "").strip()

    if not post_id:
        return Response(
            {"error": "post_id required"},
            status=400
        )

    # Validate `reason` against the model's declared choices. Without this
    # check the endpoint silently accepted an empty string or arbitrary text
    # and persisted it, contradicting the `choices=REPORT_REASONS` constraint
    # on PostReport.reason and leaving uncategorisable rows in the
    # moderation queue.
    valid_reasons = {code for code, _ in PostReport.REPORT_REASONS}
    if reason not in valid_reasons:
        return Response(
            {
                "error": "Invalid reason",
                "allowed": sorted(valid_reasons),
            },
            status=400,
        )

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
    # 🚫 PREVENT DUPLICATE REPORTS
    # --------------------------------------------------
    if PostReport.objects.filter(
        reporter=request.user,
        post=post
    ).exists():
        return Response(
            {"error": "Already reported"},
            status=400
        )

    # --------------------------------------------------
    # 📝 CREATE REPORT
    # --------------------------------------------------
    report = PostReport.objects.create(
        reporter=request.user,
        post=post,
        reason=reason
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
def report_user(request):
    reported_user_id = request.data.get("user_id")
    reason = request.data.get("reason")
    details = request.data.get("details", "")

    if not reported_user_id or not reason:
        return Response(
            {"error": "user_id and reason required"},
            status=400
        )

    if int(reported_user_id) == request.user.id:
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

    return Response(
        {"status": "reported"},
        status=201
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def report_page(request):
    page_id = request.data.get("page_id")
    reason = request.data.get("reason")
    details = request.data.get("details", "").strip()

    if not page_id or not reason:
        return Response(
            {"error": "page_id and reason are required"},
            status=400
        )

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

    return Response(
        {"status": "reported"},
        status=201
    )
