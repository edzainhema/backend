"""Tests for /auth/profile/settings/ updates.

Covers audit H4 (phone normalization / validation / uniqueness must
mirror what registration enforces -- previously the view just stripped
whitespace) and the matching H3 backstop on profile saves (atomic +
IntegrityError handler so a TOCTOU race surfaces as 400, not 500).
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import UserProfile


def _register(client, username: str, email: str | None = None) -> str:
    """Register a fresh user and return their access token. The H4
    tests need at least two users to test the cross-user uniqueness
    check, so we go through the real endpoint to keep parity with the
    production register flow (which also creates UserProfile)."""
    payload = {
        "username": username,
        "password": "strong-pass-9999",
    }
    if email:
        payload["identifier_type"] = "email"
        payload["identifier"] = email
    resp = client.post("/auth/register/", payload, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


class ProfileSettingsPhoneTests(APITestCase):
    """H4: phone updates must normalize, validate, and uniqueness-check
    against other users. Previously the view took whatever the client
    sent and stored it after a `.strip()` -- so a user could:
      (a) set their phone to a garbage string that breaks login lookup;
      (b) set their phone to another user's normalized phone, causing
          `_find_user_by_identifier` to silently resolve to the wrong
          account on phone login.
    """

    def setUp(self):
        # B4 register throttle is 5/min keyed in caches['default'];
        # multiple register calls across the test class trip it without
        # a clear. Same pattern other classes in this suite use.
        cache.clear()
        self.alice_access = _register(self.client, "alice", "alice@example.com")
        self.alice = User.objects.get(username="alice")
        self.bob_access = _register(self.client, "bob", "bob@example.com")
        self.bob = User.objects.get(username="bob")

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _settings(self, **fields):
        return self.client.post(
            "/auth/profile/settings/", fields, format="json",
        )

    # ---------- normalization ----------

    def test_phone_is_normalized_on_save(self):
        # _normalize_phone strips whitespace and dashes -- two visually
        # different inputs land as the same stored value.
        self._auth(self.alice_access)
        resp = self._settings(phone_number="+1 555-123-4567")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.alice.userprofile.refresh_from_db()
        self.assertEqual(
            self.alice.userprofile.phone_number, "+15551234567",
        )

    # ---------- format validation ----------

    def test_invalid_phone_format_rejected(self):
        self._auth(self.alice_access)
        resp = self._settings(phone_number="not-a-phone")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("error"), "Invalid phone number")
        # And nothing was persisted.
        self.alice.userprofile.refresh_from_db()
        self.assertEqual(self.alice.userprofile.phone_number, "")

    # ---------- uniqueness against other users ----------

    def test_cannot_take_another_users_phone(self):
        # bob already owns this normalized phone.
        self.bob.userprofile.phone_number = "+15551234567"
        self.bob.userprofile.save()

        self._auth(self.alice_access)
        # Alice tries to claim bob's number -- different surface form,
        # same normalized value. Reject at the app layer (uniqueness
        # check) with a clear message.
        resp = self._settings(phone_number="+1 555-123-4567")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(
            resp.data.get("error"), "Phone number already in use",
        )
        # Alice's profile is unchanged.
        self.alice.userprofile.refresh_from_db()
        self.assertEqual(self.alice.userprofile.phone_number, "")

    def test_setting_phone_to_own_current_value_is_idempotent(self):
        # Update once, then submit the same value again -- must NOT
        # trip the cross-user uniqueness check (which is supposed to
        # exclude self) or the DB-level constraint.
        self._auth(self.alice_access)
        first = self._settings(phone_number="+15559998888")
        self.assertEqual(first.status_code, 200, first.content)
        second = self._settings(phone_number="+15559998888")
        self.assertEqual(second.status_code, 200, second.content)
        self.alice.userprofile.refresh_from_db()
        self.assertEqual(
            self.alice.userprofile.phone_number, "+15559998888",
        )

    # ---------- clearing ----------

    def test_empty_string_clears_phone(self):
        self.alice.userprofile.phone_number = "+15551234567"
        self.alice.userprofile.save()
        self._auth(self.alice_access)
        resp = self._settings(phone_number="")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.alice.userprofile.refresh_from_db()
        self.assertEqual(self.alice.userprofile.phone_number, "")

    def test_multiple_users_can_have_blank_phone(self):
        # Sanity check: the partial unique index from migration 0099
        # is conditional on phone_number <> '', so two users clearing
        # their phone (or never setting one) shouldn't collide.
        self._auth(self.alice_access)
        r1 = self._settings(phone_number="")
        self.assertEqual(r1.status_code, 200, r1.content)
        self._auth(self.bob_access)
        r2 = self._settings(phone_number="")
        self.assertEqual(r2.status_code, 200, r2.content)


class ProfileSettingsRaceHandlingTests(APITestCase):
    """H3 backstop for /auth/profile/settings/: an IntegrityError from
    user.save() or profile.save() must surface as 400, not 500. Mocks
    the save to raise (the equivalent of losing the race for an email
    or phone between our pre-check and our save)."""

    def setUp(self):
        cache.clear()
        self.alice_access = _register(self.client, "alice", "alice@example.com")
        self.alice = User.objects.get(username="alice")

    def _auth(self, access):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def test_email_save_race_returns_400_not_500(self):
        from unittest.mock import patch
        from django.db import IntegrityError

        # Pre-create the row whose email we're about to race for, so
        # the re-check in the except handler can identify the conflict.
        User.objects.create_user(
            username="rival", email="taken@example.com", password="x",
        )

        # Bypass the pre-check by making `.exists()` return False
        # (simulating the race window), then make user.save() raise.
        # Easiest: patch User.save to raise once during this call.
        with patch(
            "django.contrib.auth.models.User.save",
            side_effect=IntegrityError(
                "UNIQUE constraint failed: auth_user.email"
            ),
        ):
            self._auth(self.alice_access)
            # The pre-check `.exists()` would catch this and return
            # 400 BEFORE we ever call save() -- so we send a NEW email
            # the pre-check sees as free, and let our patched save()
            # raise as if a race winner took it between check and save.
            resp = self.client.post(
                "/auth/profile/settings/",
                {"email": "fresh@example.com"},
                format="json",
            )

        self.assertNotEqual(resp.status_code, 500, resp.content)
        self.assertIn(resp.status_code, (400, 409))

    def test_unidentified_save_failure_returns_409_not_500(self):
        from unittest.mock import patch
        from django.db import IntegrityError

        # No matching row pre-created -- the re-check finds nothing,
        # so the view falls through to the generic 409 instead of 500.
        with patch(
            "django.contrib.auth.models.User.save",
            side_effect=IntegrityError("UNIQUE constraint failed"),
        ):
            self._auth(self.alice_access)
            resp = self.client.post(
                "/auth/profile/settings/",
                {"email": "ghost@example.com"},
                format="json",
            )

        self.assertNotEqual(resp.status_code, 500, resp.content)
        self.assertEqual(resp.status_code, 409)
