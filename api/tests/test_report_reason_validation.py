"""Regression tests for audit M3: report_user / report_page validate `reason`
against the model's REPORT_REASONS, like report_post already did.

DRF function views don't run model `full_clean`, so the
``choices=REPORT_REASONS`` constraint isn't enforced at save time. Before the
fix, report_user/report_page accepted any non-empty string — including one
longer than the 30-char column, which raises DataError → 500 on Postgres.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from api.models import Page, PageReport, Post, PostReport, UserReport


# Keep escalation a no-op so these tests exercise only the validation path.
@override_settings(MODERATION_EMAIL="", MODERATION_SLACK_WEBHOOK_URL="")
class ReportReasonValidationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.reporter = User.objects.create_user(username="reporter", password="x")
        self.other = User.objects.create_user(username="other", password="x")
        self.post = Post.objects.create(user=self.other, description="hi")
        self.page = Page.objects.create(owner=self.other, name="A Page")
        self.client.force_authenticate(self.reporter)

    # ---- report_user -------------------------------------------------------

    def test_user_valid_reason_accepted(self):
        resp = self.client.post(
            "/users/report/",
            {"user_id": self.other.id, "reason": "spam"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(
            UserReport.objects.filter(
                reporter=self.reporter, reported_user=self.other, reason="spam"
            ).exists()
        )

    def test_user_invalid_reason_rejected(self):
        resp = self.client.post(
            "/users/report/",
            {"user_id": self.other.id, "reason": "banana"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("allowed", resp.data)
        self.assertFalse(
            UserReport.objects.filter(
                reporter=self.reporter, reported_user=self.other
            ).exists()
        )

    def test_user_overlong_reason_rejected_not_500(self):
        """A >30-char reason is rejected with 400 (membership check) instead of
        reaching the DB and raising DataError → 500 on Postgres."""
        resp = self.client.post(
            "/users/report/",
            {"user_id": self.other.id, "reason": "x" * 200},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_user_missing_reason_rejected(self):
        resp = self.client.post(
            "/users/report/", {"user_id": self.other.id}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_user_non_numeric_id_is_400_not_500(self):
        """L1: a non-numeric user_id must return 400, not raise ValueError →
        500. Send a valid reason so we reach the id-coercion path."""
        resp = self.client.post(
            "/users/report/",
            {"user_id": "abc", "reason": "spam"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    # ---- report_page -------------------------------------------------------

    def test_page_valid_reason_accepted(self):
        resp = self.client.post(
            "/pages/report/",
            {"page_id": self.page.id, "reason": "impersonation"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(
            PageReport.objects.filter(
                reporter=self.reporter, page=self.page, reason="impersonation"
            ).exists()
        )

    def test_page_invalid_reason_rejected(self):
        resp = self.client.post(
            "/pages/report/",
            {"page_id": self.page.id, "reason": "not-a-reason"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("allowed", resp.data)
        self.assertFalse(
            PageReport.objects.filter(reporter=self.reporter, page=self.page).exists()
        )

    def test_page_overlong_reason_rejected_not_500(self):
        resp = self.client.post(
            "/pages/report/",
            {"page_id": self.page.id, "reason": "y" * 200},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    # ---- report_post (unchanged behaviour, now via the shared helper) ------

    def test_post_valid_reason_still_accepted(self):
        resp = self.client.post(
            "/posts/report/",
            {"post_id": self.post.id, "reason": "spam"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(
            PostReport.objects.filter(reporter=self.reporter, post=self.post).exists()
        )

    def test_post_duplicate_report_rejected_single_row(self):
        """L2: the get_or_create refactor still dedupes — a second report of the
        same post returns 400 (not 500) and leaves exactly one row."""
        first = self.client.post(
            "/posts/report/",
            {"post_id": self.post.id, "reason": "spam"},
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.content)
        second = self.client.post(
            "/posts/report/",
            {"post_id": self.post.id, "reason": "violence"},
            format="json",
        )
        self.assertEqual(second.status_code, 400, second.content)
        self.assertEqual(
            PostReport.objects.filter(reporter=self.reporter, post=self.post).count(),
            1,
        )

    def test_post_invalid_reason_still_rejected(self):
        resp = self.client.post(
            "/posts/report/",
            {"post_id": self.post.id, "reason": "nope"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("allowed", resp.data)
