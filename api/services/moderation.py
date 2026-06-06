"""Active escalation for user reports (audit H4).

Persisting a ``*Report`` row is necessary but not sufficient: nudity / violence /
hate / scam / impersonation reports need a human promptly, and "a moderator
might open Django admin eventually" is not a Trust-&-Safety SLA. When a report is
filed this module fires an alert to a moderation channel — email (via the
already-wired SES backend) and/or a Slack webhook — with:

  * a [SEVERE] tag for the high-priority reason codes, and
  * coalescing so a brigade against one target can't melt the channel: at most
    one alert per (report-kind, target, severe?) per
    ``MODERATION_ALERT_COOLDOWN_S``.

Everything here is BEST-EFFORT and must never break report creation. The view
enqueues it via Celery (``tasks.notify_moderation``) so it's off the request
path, and every outbound call below is wrapped so a flaky SES / Slack endpoint
is logged and swallowed, never raised.

Both channels are optional. With neither ``MODERATION_EMAIL`` nor
``MODERATION_SLACK_WEBHOOK_URL`` configured, reports still persist and show in
admin — there is simply no push alert (a clean no-op, logged once at debug).
"""
import json
import logging
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

# Reason codes (across all three *Report models) that warrant priority routing.
# A superset of every model's REPORT_REASONS severe entries, so one set covers
# post / user / page reports.
SEVERE_REASONS = frozenset({
    "nudity", "violence", "hate", "harassment", "scam", "impersonation",
})


def enqueue_report_escalation(
    *, kind, report_id, reason, target_id, target_label, reporter_username,
    details="",
):
    """Hand a freshly-created report to the moderation-alert Celery task.

    Called from the report views right after the row is written. Wrapped so an
    enqueue failure (broker down, import error) can never turn a successful
    report into a 500 — the row is already saved and visible in admin; the alert
    is an enhancement, not part of the write contract.
    """
    try:
        from ..tasks import notify_moderation
        notify_moderation.delay(
            kind, report_id, target_id, target_label, reason,
            reporter_username, details or "",
        )
    except Exception:
        logger.exception("[moderation] failed to enqueue escalation for %s #%s", kind, report_id)


def escalate_report(
    *, kind, report_id, target_id, target_label, reason, reporter_username,
    details="",
):
    """Send the moderation alert (runs on the Celery worker).

    Idempotent-ish under floods via a per-(kind, target, severity) cooldown:
    severe reports use a separate cache key from non-severe ones, so a severe
    report still escalates once per window even while a spam brigade against the
    same target is being coalesced.
    """
    severe = reason in SEVERE_REASONS
    cooldown = getattr(settings, "MODERATION_ALERT_COOLDOWN_S", 600)

    # cache.add returns True only if the key was absent — an atomic "claim this
    # window" check on the Redis/locmem backend, so concurrent reports collapse
    # to one alert instead of racing.
    claim_key = f"modalert:{kind}:{target_id}:{'sev' if severe else 'norm'}"
    if cooldown and not cache.add(claim_key, 1, timeout=cooldown):
        logger.debug("[moderation] coalesced alert for %s target=%s", kind, target_id)
        return

    prefix = "[SEVERE] " if severe else ""
    subject = f"{prefix}New {kind} report: {reason}"
    body_lines = [
        f"A new {kind} report was filed.",
        "",
        f"Target:   {target_label}",
        f"Reason:   {reason}{'  (priority)' if severe else ''}",
        f"Reporter: @{reporter_username}",
        f"Report:   {kind} report #{report_id}",
    ]
    if details:
        body_lines += ["", "Details:", details]
    if cooldown:
        body_lines += [
            "",
            f"(Further reports about this target are coalesced for "
            f"{cooldown}s — check the admin queue for the full count.)",
        ]
    body = "\n".join(body_lines)

    _send_email(subject, body)
    _post_slack(subject, body, severe=severe)


def _send_email(subject, body):
    to_addr = getattr(settings, "MODERATION_EMAIL", "") or ""
    if not to_addr:
        return
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[to_addr],
            fail_silently=False,
        )
    except Exception:
        logger.exception("[moderation] SES alert send failed")


def _post_slack(subject, body, *, severe=False):
    webhook = getattr(settings, "MODERATION_SLACK_WEBHOOK_URL", "") or ""
    if not webhook:
        return
    payload = json.dumps({"text": f"*{subject}*\n```{body}```"}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload, headers={"Content-Type": "application/json"},
    )
    try:
        # Short timeout: a hung webhook must not tie up the worker.
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        logger.exception("[moderation] Slack alert post failed")
