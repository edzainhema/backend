"""Tests for the M5 email-verification flow.

Backend-only deliverable (no frontend screen yet): registration with
an email triggers a verification mail; recipients confirm via uid+token
(or paste-able code). Social signups skip the flow entirely because the
provider already verified the address; phone-only signups skip because
there's no email to verify. Email changes via /auth/profile/settings/
reset email_verified and trigger a fresh verification mail.

Mirrors the password-reset test shape from B3.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test.utils import override_settings
from rest_framework.test import APITestCase

from api.models import UserProfile


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RegistrationSendsVerificationEmailTests(APITestCase):
    """A fresh email signup must receive a verification mail."""

    def setUp(self):
        cache.clear()
        from django.core import mail
        mail.outbox = []

    def test_email_signup_sends_verification_mail(self):
        from django.core import mail
        resp = self.client.post("/auth/register/", {
            "username": "alice",
            "password": "strong-pass-9999",
            "identifier_type": "email",
            "identifier": "alice@example.com",
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        # The verification mail landed in the outbox.
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["alice@example.com"])
        self.assertIn("Verify", msg.subject)
        # Email contains both a deep link and a paste-able code.
        self.assertIn("Code:", msg.body)
        self.assertIn("verify-email", msg.body)

    def test_phone_only_signup_sends_no_mail(self):
        from django.core import mail
        resp = self.client.post("/auth/register/", {
            "username": "bob",
            "password": "strong-pass-9999",
            "identifier_type": "phone",
            "identifier": "+15551234567",
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(mail.outbox), 0)

    def test_user_starts_unverified(self):
        """Newly registered users are unverified until they confirm."""
        resp = self.client.post("/auth/register/", {
            "username": "carol",
            "password": "strong-pass-9999",
            "identifier_type": "email",
            "identifier": "carol@example.com",
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        user = User.objects.get(username="carol")
        self.assertFalse(user.userprofile.email_verified)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailVerificationConfirmTests(APITestCase):
    """Covers /auth/email-verification-confirm/: token validation,
    success flipping the flag, replay rejection, generic errors."""

    def setUp(self):
        cache.clear()
        from django.core import mail
        reg = self.client.post("/auth/register/", {
            "username": "dave",
            "password": "strong-pass-9999",
            "identifier_type": "email",
            "identifier": "dave@example.com",
        }, format="json")
        self.assertEqual(reg.status_code, 200, reg.content)
        self.dave = User.objects.get(username="dave")
        # Extract uid + token from the mail the registration triggered.
        body = mail.outbox[-1].body
        code_line = next(line for line in body.splitlines() if "Code:" in line)
        code = code_line.split("Code:")[1].strip()
        self.uid, _, self.token = code.partition(".")
        mail.outbox = []

    def test_valid_token_marks_verified(self):
        resp = self.client.post(
            "/auth/email-verification-confirm/",
            {"uid": self.uid, "token": self.token},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.dave.userprofile.refresh_from_db()
        self.assertTrue(self.dave.userprofile.email_verified)

    def test_code_form_accepted(self):
        """Paste-from-email convenience: single `code` field works
        equivalently to (uid, token) -- mirrors password-reset confirm."""
        resp = self.client.post(
            "/auth/email-verification-confirm/",
            {"code": f"{self.uid}.{self.token}"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.dave.userprofile.refresh_from_db()
        self.assertTrue(self.dave.userprofile.email_verified)

    def test_token_cannot_be_replayed(self):
        """A successful verify flips email_verified=True, which is
        mixed into the token's hash salt -- the same token doesn't
        validate a second time."""
        first = self.client.post(
            "/auth/email-verification-confirm/",
            {"uid": self.uid, "token": self.token},
            format="json",
        )
        self.assertEqual(first.status_code, 200, first.content)

        replay = self.client.post(
            "/auth/email-verification-confirm/",
            {"uid": self.uid, "token": self.token},
            format="json",
        )
        self.assertEqual(replay.status_code, 400, replay.content)
        self.assertIn("Invalid or expired", replay.data.get("error", ""))

    def test_invalid_token_rejected(self):
        resp = self.client.post(
            "/auth/email-verification-confirm/",
            {"uid": self.uid, "token": "bogus-token-string"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Invalid or expired", resp.data.get("error", ""))

    def test_invalid_uid_rejected_with_same_message(self):
        """Bad uid and bad token return the SAME message so the
        endpoint can't be used to enumerate accounts."""
        resp = self.client.post(
            "/auth/email-verification-confirm/",
            {"uid": "not-a-uid", "token": self.token},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Invalid or expired", resp.data.get("error", ""))

    def test_missing_fields_rejected(self):
        resp = self.client.post(
            "/auth/email-verification-confirm/", {}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailVerificationRequestResendTests(APITestCase):
    """Covers /auth/email-verification-request/: re-send for the
    currently-authenticated user."""

    def setUp(self):
        cache.clear()
        from django.core import mail
        reg = self.client.post("/auth/register/", {
            "username": "eve",
            "password": "strong-pass-9999",
            "identifier_type": "email",
            "identifier": "eve@example.com",
        }, format="json")
        self.assertEqual(reg.status_code, 200, reg.content)
        self.access = reg.data["access"]
        self.eve = User.objects.get(username="eve")
        mail.outbox = []

    def test_resend_sends_new_email(self):
        from django.core import mail
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        resp = self.client.post(
            "/auth/email-verification-request/", {}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(mail.outbox), 1)

    def test_resend_requires_auth(self):
        from django.core import mail
        resp = self.client.post(
            "/auth/email-verification-request/", {}, format="json",
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(len(mail.outbox), 0)

    def test_already_verified_user_gets_200_but_no_mail(self):
        """Generic 200 either way (don't leak the verification state
        via response shape); the service silently no-ops on
        already-verified accounts."""
        from django.core import mail
        # Flip verified=True before the resend call.
        self.eve.userprofile.email_verified = True
        self.eve.userprofile.save(update_fields=["email_verified"])

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        resp = self.client.post(
            "/auth/email-verification-request/", {}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailChangeResetsVerificationTests(APITestCase):
    """M5: changing email in profile-settings clears email_verified
    and triggers a fresh verification mail to the new address."""

    def setUp(self):
        cache.clear()
        reg = self.client.post("/auth/register/", {
            "username": "frank",
            "password": "strong-pass-9999",
            "identifier_type": "email",
            "identifier": "frank@example.com",
        }, format="json")
        self.assertEqual(reg.status_code, 200, reg.content)
        self.access = reg.data["access"]
        self.frank = User.objects.get(username="frank")
        # Skip past the initial verify by flipping the flag directly.
        self.frank.userprofile.email_verified = True
        self.frank.userprofile.save(update_fields=["email_verified"])
        from django.core import mail
        mail.outbox = []

    def _auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")

    def test_email_change_clears_verified_and_sends_mail(self):
        from django.core import mail
        self._auth()
        resp = self.client.post(
            "/auth/profile/settings/",
            {"email": "frank-new@example.com"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.frank.refresh_from_db()
        self.frank.userprofile.refresh_from_db()
        self.assertEqual(self.frank.email, "frank-new@example.com")
        self.assertFalse(self.frank.userprofile.email_verified)
        # Verification mail went to the NEW address.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["frank-new@example.com"])

    def test_same_email_resubmit_does_not_reset_verified(self):
        """Posting the same email back must NOT clear `email_verified`
        or trigger a redundant mail -- otherwise a user who saves
        unrelated settings (bio, phone, etc.) with email unchanged
        loses their verified status every save."""
        from django.core import mail
        self._auth()
        # Resubmit identical email.
        resp = self.client.post(
            "/auth/profile/settings/",
            {"email": "frank@example.com"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.frank.userprofile.refresh_from_db()
        self.assertTrue(self.frank.userprofile.email_verified)
        self.assertEqual(len(mail.outbox), 0)

    def test_clearing_email_clears_verified(self):
        """Setting email to "" clears the address; verified must go
        with it -- otherwise the user could re-set the email later
        and "inherit" verified status from the cleared period."""
        self._auth()
        resp = self.client.post(
            "/auth/profile/settings/", {"email": ""}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.frank.refresh_from_db()
        self.frank.userprofile.refresh_from_db()
        self.assertEqual(self.frank.email, "")
        self.assertFalse(self.frank.userprofile.email_verified)
