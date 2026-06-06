"""Tests for audit H4: report triage state + active escalation.

Covers:
  * the shared ReportTriage fields (status/handled_by/resolved_at) and defaults,
  * the escalation service (severe tagging, flood-coalescing, channel gating),
  * end-to-end that filing a report fires exactly one alert (Celery runs eager
    in tests, so .delay() executes inline).

The Django test runner swaps EMAIL_BACKEND for the locmem backend, so
``django.core.mail.outbox`` captures what SES would have sent.
"""
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from api.models import UserReport
from api.services.moderation import escalate_report


class ReportTriageModelTests(APITestCase):
    def test_new_report_defaults_to_open_unhandled(self):
        a = User.objects.create_user(username="a", password="x")
        b = User.objects.create_user(username="b", password="x")
        r = UserReport.objects.create(reporter=a, reported_user=b, reason="spam")
        self.assertEqual(r.status, UserReport.STATUS_OPEN)
        self.assertTrue(r.is_open)
        self.assertIsNone(r.handled_by)
        self.assertIsNone(r.resolved_at)

    def test_resolved_report_not_open(self):
        a = User.objects.create_user(username="a", password="x")
        b = User.objects.create_user(username="b", password="x")
        r = UserReport.objects.create(
            reporter=a, reported_user=b, reason="spam",
            status=UserReport.STATUS_DISMISSED,
        )
        self.assertFalse(r.is_open)


@override_settings(
    MODERATION_EMAIL="trust@here-social.test",
    MODERATION_SLACK_WEBHOOK_URL="",
    MODERATION_ALERT_COOLDOWN_S=600,
)
class EscalationServiceTests(APITestCase):
    def setUp(self):
        cache.clear()
        mail.outbox = []

    def _escalate(self, *, reason="spam", target_id=1, kind="user"):
        escalate_report(
            kind=kind, report_id=1, target_id=target_id,
            target_label=f"user @victim ({target_id})", reason=reason,
            reporter_username="reporter", details="",
        )

    def test_non_severe_sends_one_email_without_severe_tag(self):
        self._escalate(reason="spam")
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("[SEVERE]", mail.outbox[0].subject)

    def test_severe_reason_tagged(self):
        self._escalate(reason="violence")
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].subject.startswith("[SEVERE]"))

    def test_flood_against_same_target_is_coalesced(self):
        # Two non-severe reports about the same target within the window ->
        # a single alert (the brigade-protection coalescing).
        self._escalate(reason="spam", target_id=42)
        self._escalate(reason="spam", target_id=42)
        self.assertEqual(len(mail.outbox), 1)

    def test_severe_not_coalesced_by_nonsevere(self):
        # A severe report uses a separate cooldown key, so it still escalates
        # even if a non-severe alert for the same target just fired.
        self._escalate(reason="spam", target_id=7)
        self._escalate(reason="violence", target_id=7)
        self.assertEqual(len(mail.outbox), 2)

    def test_different_targets_each_alert(self):
        self._escalate(reason="spam", target_id=1)
        self._escalate(reason="spam", target_id=2)
        self.assertEqual(len(mail.outbox), 2)

    @override_settings(MODERATION_ALERT_COOLDOWN_S=0)
    def test_cooldown_zero_disables_coalescing(self):
        self._escalate(reason="spam", target_id=9)
        self._escalate(reason="spam", target_id=9)
        self.assertEqual(len(mail.outbox), 2)


@override_settings(MODERATION_EMAIL="", MODERATION_SLACK_WEBHOOK_URL="")
class EscalationDisabledTests(APITestCase):
    def setUp(self):
        cache.clear()
        mail.outbox = []

    def test_no_channels_configured_is_silent_noop(self):
        escalate_report(
            kind="post", report_id=1, target_id=1, target_label="post #1",
            reason="violence", reporter_username="r", details="",
        )
        self.assertEqual(len(mail.outbox), 0)


@override_settings(
    MODERATION_EMAIL="trust@here-social.test",
    MODERATION_SLACK_WEBHOOK_URL="",
)
class ReportUserEndToEndEscalationTests(APITestCase):
    """Filing a report through the API fires exactly one alert (eager Celery)."""

    def setUp(self):
        cache.clear()
        mail.outbox = []
        self.reporter = User.objects.create_user(username="reporter", password="x")
        self.victim = User.objects.create_user(username="victim", password="x")
        self.client.force_authenticate(self.reporter)

    def test_report_user_persists_and_alerts(self):
        resp = self.client.post(
            "/users/report/",
            {"user_id": self.victim.id, "reason": "harassment"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(
            UserReport.objects.filter(
                reporter=self.reporter, reported_user=self.victim
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].subject.startswith("[SEVERE]"))
