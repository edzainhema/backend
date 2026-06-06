"""Tests for audit H3: last_seen bump on register + stale-token reaping.

The cross-account push leak (an FCM token is per app INSTALL, not per account)
is contained server-side by: register_device recording/refreshing
`Device.last_seen`, and `prune_stale_devices` deleting rows that stopped
refreshing — never a still-registering (multi-account) row.
"""
from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from api.models import Device


class RegisterDeviceLastSeenTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(username="alice", password="x")
        self.client.force_authenticate(self.alice)

    def test_register_sets_last_seen(self):
        resp = self.client.post(
            "/auth/register-device/", {"token": "tok-A"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        dev = Device.objects.get(user=self.alice, token="tok-A")
        self.assertIsNotNone(dev.last_seen)

    def test_reregister_bumps_last_seen_without_duplicating(self):
        """A repeat register must move last_seen forward (the old no-op-defaults
        version left it untouched) and must not create a second row."""
        self.client.post("/auth/register-device/", {"token": "tok-A"}, format="json")
        dev = Device.objects.get(user=self.alice, token="tok-A")
        stale = timezone.now() - timedelta(days=90)
        Device.objects.filter(pk=dev.pk).update(last_seen=stale)

        self.client.post("/auth/register-device/", {"token": "tok-A"}, format="json")
        dev.refresh_from_db()
        self.assertGreater(dev.last_seen, stale)
        self.assertEqual(
            Device.objects.filter(user=self.alice, token="tok-A").count(), 1
        )


class PruneStaleDevicesTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice", password="x")
        self.bob = User.objects.create_user(username="bob", password="x")

    def _device(self, user, token, *, days_ago):
        d = Device.objects.create(user=user, token=token)
        Device.objects.filter(pk=d.pk).update(
            last_seen=timezone.now() - timedelta(days=days_ago)
        )
        return d

    def test_reaps_stale_keeps_fresh(self):
        stale = self._device(self.alice, "stale-tok", days_ago=90)
        fresh = self._device(self.alice, "fresh-tok", days_ago=1)
        call_command("prune_stale_devices", "--max-age-days", "60", stdout=StringIO())
        self.assertFalse(Device.objects.filter(pk=stale.pk).exists())
        self.assertTrue(Device.objects.filter(pk=fresh.pk).exists())

    def test_multi_account_live_row_survives_when_sibling_reaped(self):
        """Same token shared by two accounts. The account still refreshing
        (alice, fresh) keeps its row even though the other account's stale
        sibling row for the SAME token is reaped — reaping is per (user, token),
        never a blind token-wide delete."""
        a_live = self._device(self.alice, "shared-tok", days_ago=2)
        b_dead = self._device(self.bob, "shared-tok", days_ago=120)
        call_command("prune_stale_devices", "--max-age-days", "60", stdout=StringIO())
        self.assertTrue(Device.objects.filter(pk=a_live.pk).exists())
        self.assertFalse(Device.objects.filter(pk=b_dead.pk).exists())

    def test_legacy_null_last_seen_reaped_by_created_at(self):
        """Defensive: a row whose last_seen is NULL (pre-backfill straggler)
        still ages out via created_at, so a stray NULL can't make it immortal."""
        d = Device.objects.create(user=self.alice, token="legacy-tok")
        old = timezone.now() - timedelta(days=200)
        # Force both timestamps old and last_seen NULL.
        Device.objects.filter(pk=d.pk).update(last_seen=None, created_at=old)
        call_command("prune_stale_devices", "--max-age-days", "60", stdout=StringIO())
        self.assertFalse(Device.objects.filter(pk=d.pk).exists())

    def test_dry_run_deletes_nothing(self):
        stale = self._device(self.alice, "stale-tok", days_ago=90)
        call_command(
            "prune_stale_devices", "--max-age-days", "60", "--dry-run",
            stdout=StringIO(),
        )
        self.assertTrue(Device.objects.filter(pk=stale.pk).exists())

    def test_refuses_nonpositive_window(self):
        """A 0/negative window would reap live rows; the command must refuse."""
        live = self._device(self.alice, "tok", days_ago=0)
        call_command(
            "prune_stale_devices", "--max-age-days", "0",
            stdout=StringIO(), stderr=StringIO(),
        )
        self.assertTrue(Device.objects.filter(pk=live.pk).exists())
