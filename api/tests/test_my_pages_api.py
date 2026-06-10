"""Tests for /pages/mine/ (`list_my_pages`).

Backs the Profile → "My pages" screen: lists the pages the signed-in user
*created* (owner == viewer), newest first, with optional name search and
offset/limit pagination. Verifies auth gating, owner-scoping (you never see
someone else's pages), ordering, search filtering, and the has_more flag.
"""
import time

from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import Page


def _register(client, username: str) -> str:
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "strong-pass-9999",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


class ListMyPagesTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.owner_access = _register(self.client, "owner")
        self.owner = User.objects.get(username="owner")
        self.other = User.objects.create_user(
            username="other", password="x"
        )

        # Created oldest -> newest so we can assert reverse-chronological order.
        self.alpha = Page.objects.create(owner=self.owner, name="Alpha Club")
        self.beta = Page.objects.create(owner=self.owner, name="Beta Society")
        self.gamma = Page.objects.create(owner=self.owner, name="Gamma Group")

        # A page owned by someone else must never appear in the viewer's list.
        Page.objects.create(owner=self.other, name="Not Yours")

    def _auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.owner_access}")

    def test_requires_auth(self):
        resp = self.client.get("/pages/mine/")
        self.assertEqual(resp.status_code, 401)

    def test_lists_only_owned_pages_newest_first(self):
        self._auth()
        resp = self.client.get("/pages/mine/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("results", resp.data)
        self.assertIn("has_more", resp.data)

        names = [p["name"] for p in resp.data["results"]]
        # Owner's three pages only, newest (gamma) first.
        self.assertEqual(names, ["Gamma Group", "Beta Society", "Alpha Club"])
        self.assertNotIn("Not Yours", names)
        self.assertFalse(resp.data["has_more"])

    def test_search_filters_by_name(self):
        self._auth()
        resp = self.client.get("/pages/mine/", {"search": "beta"})
        self.assertEqual(resp.status_code, 200, resp.content)
        names = [p["name"] for p in resp.data["results"]]
        self.assertEqual(names, ["Beta Society"])

    def test_pagination_has_more(self):
        self._auth()
        resp = self.client.get("/pages/mine/", {"limit": 2})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["results"]), 2)
        self.assertTrue(resp.data["has_more"])

        resp2 = self.client.get("/pages/mine/", {"limit": 2, "offset": 2})
        self.assertEqual(len(resp2.data["results"]), 1)
        self.assertFalse(resp2.data["has_more"])
