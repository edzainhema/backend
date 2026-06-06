"""Tests for audit L5: token-less unregister-device no longer nukes all devices.

The endpoint used to treat a missing `token` as "delete ALL of this user's
Device rows", so a signOut() that forgot the token would silently kill push on
the user's other phones. A token is now required by default; the all-devices
nuke is explicit opt-in (`all_devices=true`).
"""
from django.contrib.auth.models import User
from rest_framework.test import APITestCase

from api.models import Device


class UnregisterDeviceTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.client.force_authenticate(self.user)
        # Two phones for the same account.
        self.phone_a = Device.objects.create(user=self.user, token="tok-A")
        self.phone_b = Device.objects.create(user=self.user, token="tok-B")

    def test_token_drops_only_that_device(self):
        resp = self.client.post(
            "/auth/unregister-device/", {"token": "tok-A"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(Device.objects.filter(pk=self.phone_a.pk).exists())
        # The OTHER phone's push registration must survive.
        self.assertTrue(Device.objects.filter(pk=self.phone_b.pk).exists())

    def test_missing_token_is_400_and_deletes_nothing(self):
        """The footgun: no token, no explicit opt-in -> reject, touch nothing."""
        resp = self.client.post("/auth/unregister-device/", {}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Device.objects.filter(user=self.user).count(), 2)

    def test_all_devices_opt_in_deletes_everything(self):
        resp = self.client.post(
            "/auth/unregister-device/", {"all_devices": True}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Device.objects.filter(user=self.user).count(), 0)

    def test_all_devices_accepts_string_true(self):
        """Form/string clients send 'true', not a JSON bool."""
        resp = self.client.post(
            "/auth/unregister-device/", {"all_devices": "true"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Device.objects.filter(user=self.user).count(), 0)

    def test_token_wins_over_all_devices_when_both_sent(self):
        """Token is the more specific, safer scope — only that device drops."""
        resp = self.client.post(
            "/auth/unregister-device/",
            {"token": "tok-A", "all_devices": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(Device.objects.filter(pk=self.phone_a.pk).exists())
        self.assertTrue(Device.objects.filter(pk=self.phone_b.pk).exists())
